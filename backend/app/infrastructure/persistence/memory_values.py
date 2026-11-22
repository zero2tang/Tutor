"""跟踪记忆文档的嵌套修改；不把正文重新映射成数据库列。"""

from copy import deepcopy


def tracked(value, changed):
    if isinstance(value, dict):
        return DocumentDict(value, changed)
    if isinstance(value, list):
        return DocumentList(value, changed)
    return deepcopy(value)


class DocumentDict(dict):
    def __init__(self, value, changed):
        self.changed = changed
        super().__init__((key, tracked(item, changed)) for key, item in value.items())

    def __deepcopy__(self, memo):
        return {key: deepcopy(value, memo) for key, value in self.items()}

    def __setitem__(self, key, value):
        super().__setitem__(key, tracked(value, self.changed))
        self.changed()

    def __delitem__(self, key):
        super().__delitem__(key)
        self.changed()

    def update(self, *args, **kwargs):
        for key, value in dict(*args, **kwargs).items():
            self[key] = value

    def setdefault(self, key, default=None):
        if key not in self:
            self[key] = default
        return self[key]

    def pop(self, key, *default):
        result = super().pop(key, *default)
        self.changed()
        return result

    def popitem(self):
        result = super().popitem()
        self.changed()
        return result

    def clear(self):
        super().clear()
        self.changed()

    def __ior__(self, value):
        self.update(value)
        return self


class DocumentList(list):
    def __init__(self, value, changed):
        self.changed = changed
        super().__init__(tracked(item, changed) for item in value)

    def __deepcopy__(self, memo):
        return [deepcopy(value, memo) for value in self]

    def __setitem__(self, key, value):
        value = ([tracked(item, self.changed) for item in value] if isinstance(key, slice)
                 else tracked(value, self.changed))
        super().__setitem__(key, value)
        self.changed()

    def __delitem__(self, key):
        super().__delitem__(key)
        self.changed()

    def append(self, value):
        super().append(tracked(value, self.changed))
        self.changed()

    def extend(self, values):
        super().extend(tracked(value, self.changed) for value in values)
        self.changed()

    def insert(self, index, value):
        super().insert(index, tracked(value, self.changed))
        self.changed()

    def pop(self, index=-1):
        value = super().pop(index)
        self.changed()
        return value

    def remove(self, value):
        super().remove(value)
        self.changed()

    def clear(self):
        super().clear()
        self.changed()

    def reverse(self):
        super().reverse()
        self.changed()

    def sort(self, *args, **kwargs):
        super().sort(*args, **kwargs)
        self.changed()

    def __iadd__(self, value):
        self.extend(value)
        return self

    def __imul__(self, value):
        super().__imul__(value)
        self.changed()
        return self
