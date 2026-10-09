"""Count complete recorded request contexts and responses without model weights."""
from .conversation import content_text
from .tokenizer import CHAT_KWARGS


class LengthCounter:
    """一次加载 tokenizer，多条调用分别计数。input 是完整上下文，output 仅是当前文本。"""
    def __init__(self, tokenizer):
        self.tokenizer = tokenizer

    def input_lengths(self, context, *, tools=None, prompt_controls=None):
        ids = self.tokenizer.apply_chat_template(context, tools=tools, **CHAT_KWARGS, **(prompt_controls or {}))
        return {"input_tokens": len(ids)}

    def output_length(self, response_text):
        return len(self.tokenizer.encode(content_text(response_text), add_special_tokens=False, truncation=False))

