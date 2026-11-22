"""官方 Store 保存不可变正文，业务事务只发布文档指针与治理状态。"""

from contextlib import ExitStack, asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass
from collections import OrderedDict
from hashlib import sha256
import json
from threading import RLock
from urllib.parse import quote
from weakref import WeakKeyDictionary

from langgraph.store.base import BaseStore
from langgraph.store.postgres import PostgresStore
from langgraph.store.postgres.aio import AsyncPostgresStore
from psycopg import Connection
from psycopg.conninfo import make_conninfo
from sqlalchemy import event, inspect
from sqlalchemy.orm import Session, attributes, object_session

from app.infrastructure.persistence.memory_values import tracked


class MemoryDocumentError(RuntimeError):
    """正文缺失、归属不匹配或存储未装配时拒绝读取。"""


def _digest(value):
    wire = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return sha256(wire.encode("utf-8")).hexdigest()


def document_namespace(row):
    """命名空间只使用服务端持有的控制行归属，不接受模型提供的范围。"""
    student = row.student_id
    if not student:
        raise MemoryDocumentError("记忆文档缺少学生归属")
    kind = row.__tablename__
    if kind == "scope_memories":
        if row.scope_kind not in {"student", "course", "concept"}:
            raise MemoryDocumentError("记忆文档作用域无效")
        identity = (row.scope_kind, row.course_id or "", row.concept_id or "")
    elif kind == "session_concept_observations":
        identity = (row.session_id, row.course_id, row.concept_id)
    else:
        identity = (row.session_id, row.course_id)
    return tuple(quote(str(value), safe="").replace(".", "%2E") or "_"
                 for value in ("tutor", "memory", student, kind, *identity))


@dataclass
class MemoryDocumentRepository:
    store: BaseStore

    def __post_init__(self):
        self._staged = OrderedDict()
        self._lock = RLock()

    def remember_staged(self, namespace, key):
        """仅缓存本进程已成功写入的不可变版本，不缓存 SQL 发布权限。"""
        with self._lock:
            self._staged[(namespace, key)] = None
            self._staged.move_to_end((namespace, key))
            while len(self._staged) > 256:
                self._staged.popitem(last=False)

    def read(self, namespace, key):
        item = self.store.get(namespace, key)
        if item is None:
            raise MemoryDocumentError("已发布记忆文档不存在")
        value = item.value
        if value.get("namespace") != list(namespace) or key != "r1-" + _digest(value):
            raise MemoryDocumentError("记忆文档归属或内容哈希不匹配")
        if value.get("schema_version") != 1 or not isinstance(value.get("fields"), dict):
            raise MemoryDocumentError("记忆文档版本无效")
        return deepcopy(value["fields"])

    def stage(self, namespace, fields):
        """预写失败不会发布；业务回滚留下的不可见版本可独立回收。"""
        value = {"schema_version": 1, "namespace": list(namespace), "fields": deepcopy(fields)}
        key = "r1-" + _digest(value)
        with self._lock:
            if (namespace, key) in self._staged:
                return key
        existing = self.store.get(namespace, key)
        if existing is None:
            self.store.put(namespace, key, value, index=False)
        elif existing.value != value:
            raise MemoryDocumentError("不可变记忆版本发生内容冲突")
        self.remember_staged(namespace, key)
        return key


async def stage_memory_fields(store, namespace, fields, *, repository=None):
    """图在 SQL 发布事务之前用官方异步 Store 预写完整候选正文。"""
    value = {"schema_version": 1, "namespace": list(namespace), "fields": deepcopy(fields)}
    key = "r1-" + _digest(value)
    existing = await store.aget(namespace, key)
    if existing is None:
        await store.aput(namespace, key, value, index=False)
    elif existing.value != value:
        raise MemoryDocumentError("不可变记忆版本发生内容冲突")
    if repository is not None:
        repository.remember_staged(namespace, key)
    return key


_repositories = WeakKeyDictionary()
_registry_lock = RLock()


def bind_memory_document_store(engine, store):
    """只绑定指定 Engine；测试必须显式传入官方 InMemoryStore。"""
    with _registry_lock:
        if engine in _repositories:
            raise MemoryDocumentError("该数据库已绑定记忆 Store")
        repository = MemoryDocumentRepository(store)
        _repositories[engine] = repository
        return repository


def unbind_memory_document_store(engine):
    with _registry_lock:
        _repositories.pop(engine, None)


def memory_document_repository(engine):
    with _registry_lock:
        repository = _repositories.get(engine)
    if repository is None:
        raise MemoryDocumentError("数据库尚未绑定记忆 Store")
    return repository


def _repository(row):
    session = object_session(row)
    if session is None:
        raise MemoryDocumentError("未加载的记忆文档需要有效数据库会话")
    bind = session.get_bind(mapper=type(row))
    return memory_document_repository(getattr(bind, "engine", bind))


def _changed(row):
    row.__dict__["_document_modified"] = True
    if inspect(row).persistent:
        attributes.flag_dirty(row)


def _discard(row, *args):
    for key in ("_document_values", "_document_loaded_key", "_document_namespace", "_document_modified"):
        row.__dict__.pop(key, None)


def _values(row):
    key = row.document_key or ""
    cached = row.__dict__.get("_document_values")
    if cached is not None and row.__dict__.get("_document_loaded_key", "") == key:
        return cached
    fields = {name: factory() for name, factory in row.__document_fields__.items()}
    namespace = document_namespace(row) if key else None
    if key:
        stored = _repository(row).read(namespace, key)
        if set(stored) != set(fields):
            raise MemoryDocumentError("记忆文档字段与当前模型不一致")
        if any(not isinstance(stored[name], factory) for name, factory in row.__document_fields__.items()):
            raise MemoryDocumentError("记忆文档字段类型无效")
        fields.update(stored)
    values = {name: tracked(value, lambda: _changed(row)) for name, value in fields.items()}
    row.__dict__.update(_document_values=values, _document_loaded_key=key,
                        _document_namespace=namespace, _document_modified=False)
    return values


class DocumentField:
    """普通领域属性；列表与字典的嵌套修改也会触发 ORM flush。"""

    def __set_name__(self, owner, name):
        self.name = name

    def __get__(self, row, owner=None):
        return self if row is None else _values(row)[self.name]

    def __set__(self, row, value):
        if not isinstance(value, row.__document_fields__[self.name]):
            raise ValueError("记忆正文字段类型无效")
        _values(row)[self.name] = tracked(value, lambda: _changed(row))
        _changed(row)


class MemoryDocumentMixin:
    __allow_unmapped__ = True


def stage_memory_document(row):
    """图节点可显式预写；默认 flush 也调用此函数，不伪称跨 Store 原子提交。"""
    namespace = document_namespace(row)
    fields = _values(row)
    original_namespace = row.__dict__.get("_document_namespace")
    if original_namespace is not None and original_namespace != namespace:
        raise MemoryDocumentError("已发布记忆的归属不可变更")
    key = _repository(row).stage(namespace, fields)
    row.document_key = key
    row.__dict__.update(_document_loaded_key=key, _document_namespace=namespace,
                        _document_modified=False)
    return key


@event.listens_for(Session, "before_flush")
def _flush_documents(session, flush_context, instances):
    for row in set(session.new).union(session.dirty):
        if isinstance(row, MemoryDocumentMixin):
            if row.__dict__.get("_document_modified"):
                stage_memory_document(row)
            elif row.document_key:
                # 归属修改不能把旧指针搬到另一个学生或课程。
                namespace = document_namespace(row)
                if (row.__dict__.get("_document_namespace") != namespace
                        or row.__dict__.get("_document_loaded_key") != row.document_key):
                    _repository(row).read(namespace, row.document_key)


@event.listens_for(Session, "after_soft_rollback")
def _rollback_documents(session, previous_transaction):
    for row in list(session.identity_map.values()):
        if isinstance(row, MemoryDocumentMixin):
            _discard(row)


def register_memory_document_model(model):
    event.listen(model, "expire", _discard)
    event.listen(model, "refresh", lambda row, context, attrs: _discard(row))
    event.listen(model, "load", lambda row, context: _values(row), restore_load_context=True)


def memory_store_dsn(dsn):
    # 不回退 public，避免误复用另一个应用的同名官方 Store 表。
    return make_conninfo(dsn, options="-c search_path=langgraph_store")


def setup_memory_document_storage(dsn):
    """部署时显式建立官方 Store 表；应用启动不隐式执行迁移。"""
    with Connection.connect(dsn, autocommit=True) as connection:
        connection.execute("CREATE SCHEMA IF NOT EXISTS langgraph_store")
    with PostgresStore.from_conn_string(memory_store_dsn(dsn)) as store:
        store.setup()


def verify_memory_document_storage(dsn):
    """启动只读验证官方表与完整迁移版本，不替维护命令补建结构。"""
    with Connection.connect(dsn, autocommit=True, connect_timeout=10,
            options="-c default_transaction_read_only=on") as connection:
        exists = connection.execute(
            "SELECT to_regclass('langgraph_store.store'), "
            "to_regclass('langgraph_store.store_migrations')"
        ).fetchone()
        if not all(exists):
            raise MemoryDocumentError("PostgreSQL 记忆 Store 尚未初始化，请先执行维护 prepare 命令")
        versions = [row[0] for row in connection.execute(
            "SELECT v FROM langgraph_store.store_migrations ORDER BY v"
        ).fetchall()]
        if versions != list(range(len(PostgresStore.MIGRATIONS))):
            raise MemoryDocumentError("PostgreSQL 记忆 Store 迁移版本不匹配，请先执行维护 prepare 命令")
        # 版本号不能掩盖损坏的表结构，也不读取任何学生正文。
        connection.execute("SELECT prefix,key,value,created_at,updated_at,expires_at,ttl_minutes "
                           "FROM langgraph_store.store LIMIT 0")


@dataclass(frozen=True)
class MemoryDocumentStorage:
    sync_store: PostgresStore
    async_store: AsyncPostgresStore


@asynccontextmanager
async def memory_document_storage(dsn, *, engine, setup=False):
    if setup:
        setup_memory_document_storage(dsn)
    verify_memory_document_storage(dsn)
    scoped_dsn = memory_store_dsn(dsn)
    with ExitStack() as stack:
        sync_store = stack.enter_context(PostgresStore.from_conn_string(scoped_dsn,
            pool_config={"min_size": 1, "max_size": 4}))
        async with AsyncPostgresStore.from_conn_string(scoped_dsn,
                pool_config={"min_size": 1, "max_size": 4}) as async_store:
            # 同步和异步实际连接均可读，才把 Store 暴露给应用生命周期。
            sync_store.get(("tutor", "startup"), "readiness")
            await async_store.aget(("tutor", "startup"), "readiness")
            bind_memory_document_store(engine, sync_store)
            try:
                yield MemoryDocumentStorage(sync_store, async_store)
            finally:
                unbind_memory_document_store(engine)
