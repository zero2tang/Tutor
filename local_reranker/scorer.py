"""用 Qwen 的 yes/no 下一词打分；线上和离线复用相同实现。"""

from pathlib import Path

import torch
from transformers import AutoModelForCausalLM, AutoProcessor, AutoTokenizer, Qwen3VLForConditionalGeneration


SYSTEM = ('Judge whether the Document meets the requirements based on the Query and the Instruct '
          'provided. Note that the answer can only be "yes" or "no".')
TEXT_SUFFIX = "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"
MAX_TOKENS = 8192
MAX_IMAGE_PIXELS = 768 * 32 * 32
TEXT_MODEL = "Qwen3-Reranker-0.6B"
VISION_MODEL = "Qwen3-VL-Reranker-2B"
INSTRUCTIONS = {
    "exact": "Find the same math problem. Check the given numbers, conditions, and what is asked.",
    "isomorphic": "Find a math problem with the same mathematical relationships and operation structure. The numbers and story may differ.",
    "same_method": "Find a math problem that can demonstrate the same core solution method and type of goal. Similar vocabulary or topic alone is insufficient.",
}


class QwenScorer:
    def __init__(self, folder: Path, visual: bool):
        self.visual = visual
        if not torch.cuda.is_available() or not torch.cuda.is_bf16_supported():
            raise RuntimeError("Qwen 精排需要支持 BF16 的 CUDA GPU")
        args = {"local_files_only": True, "dtype": torch.bfloat16,
                "attn_implementation": "sdpa"}
        if visual:
            lm = Qwen3VLForConditionalGeneration.from_pretrained(str(folder), **args)
            self.processor = AutoProcessor.from_pretrained(str(folder), local_files_only=True, padding_side="left")
            self.tokenizer = self.processor.tokenizer
        else:
            lm = AutoModelForCausalLM.from_pretrained(str(folder), **args)
            self.tokenizer = AutoTokenizer.from_pretrained(str(folder), local_files_only=True, padding_side="left")
        self.model = lm.model.to("cuda").eval()
        yes, no = [self.tokenizer.get_vocab()[label] for label in ("yes", "no")]
        # 只计算 yes 和 no 的 logit 差，与对这两个词作 softmax 后排序等价。
        with torch.no_grad():
            self.weight = (lm.lm_head.weight[yes].float() - lm.lm_head.weight[no].float()).to("cuda")

    @staticmethod
    def media(row, label):
        parts = [{"type": "text", "text": label}]
        for image in row["images"]:
            parts.append({"type": "image", "image": image,
                          "min_pixels": 4 * 32 * 32, "max_pixels": MAX_IMAGE_PIXELS})
        parts.append({"type": "text", "text": row["text"]})
        return parts

    def multimodal_inputs(self, query, docs, instruction):
        from qwen_vl_utils import process_vision_info

        chats = [[{"role": "system", "content": [{"type": "text", "text": SYSTEM}]},
                  {"role": "user", "content": [{"type": "text", "text": "<Instruct>: " + instruction}]
                   + self.media(query, "<Query>:") + self.media(doc, "\n<Document>:")}] for doc in docs]
        texts = self.processor.apply_chat_template(chats, tokenize=False, add_generation_prompt=True)
        images, _ = process_vision_info(chats, image_patch_size=16)
        inputs = self.processor(text=texts, images=images, padding=True, truncation=False,
                                do_resize=False, return_tensors="pt")
        expected_images = sum(len(query["images"]) + len(doc["images"]) for doc in docs)
        image_grid = inputs.get("image_grid_thw")
        if (0 if image_grid is None else image_grid.shape[0]) != expected_images:
            raise ValueError("精排模型收到的图像与输入题图不一致")
        return inputs

    def text_inputs(self, query, docs, instruction):
        prefix = f"<|im_start|>system\n{SYSTEM}<|im_end|>\n<|im_start|>user\n"
        texts = [f"{prefix}<Instruct>: {instruction}\n<Query>: {query['text']}\n"
                 f"<Document>: {doc['text']}{TEXT_SUFFIX}" for doc in docs]
        return self.tokenizer(texts, padding=True, truncation=False,
                              add_special_tokens=False, return_tensors="pt")

    @torch.inference_mode()
    def score(self, query, docs, instruction, batch_size):
        values = []
        max_tokens = 0
        images_scored = 0
        for i in range(0, len(docs), batch_size):
            batch = docs[i:i + batch_size]
            inputs = self.multimodal_inputs(query, batch, instruction) if self.visual else self.text_inputs(query, batch, instruction)
            token_count = inputs["input_ids"].shape[1]
            if token_count > MAX_TOKENS:
                raise ValueError(f"题面超过 {MAX_TOKENS} tokens；禁止截断条件")
            max_tokens = max(max_tokens, token_count)
            if self.visual:
                images_scored += sum(len(query["images"]) + len(doc["images"]) for doc in batch)
            hidden = self.model(**inputs.to("cuda"), use_cache=False).last_hidden_state[:, -1].float()
            scores = hidden @ self.weight
            if not torch.isfinite(scores).all():
                raise ValueError("Qwen 精排分数不是有限值")
            values.extend(scores.cpu().tolist())
        return values, max_tokens, images_scored
