"""
কনফিগ লোডার — config.yaml থেকে non-secret সেটিংস, আর environment variable
থেকে secret জিনিসগুলো (টোকেন/key) নেয়। এই মডিউলটা ইম্পোর্ট করলেই একটা
গ্লোবাল `CFG` অবজেক্ট পাওয়া যায়।
"""

import itertools
import os

import yaml

_HERE = os.path.dirname(os.path.abspath(__file__))
_CONFIG_PATH = os.environ.get("AI_EDITOR_CONFIG", os.path.join(_HERE, "config.yaml"))


class Config:
    def __init__(self, path):
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)

        # ---- Secrets: শুধু environment variable থেকে ----
        self.telegram_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        if not self.telegram_token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN environment variable সেট করা নেই।")

        raw_keys = os.environ.get("GEMINI_API_KEYS", "")
        self.gemini_keys = [k.strip() for k in raw_keys.split(",") if k.strip()]
        if not self.gemini_keys:
            raise RuntimeError("GEMINI_API_KEYS environment variable-এ অন্তত একটা key দিতে হবে (কমা দিয়ে আলাদা করে ১০-১২টাও দেওয়া যাবে)।")
        self._key_cycle = itertools.cycle(self.gemini_keys)

        raw_allowed = os.environ.get("ALLOWED_USER_IDS", "")
        self.allowed_user_ids = {int(x) for x in raw_allowed.split(",") if x.strip().isdigit()}

        # REPO_PATH env var থাকলে yaml-এর ভ্যালু override করবে
        self.repo_path = os.environ.get("REPO_PATH", raw.get("repo_path", ""))
        if not self.repo_path or not os.path.isdir(self.repo_path):
            raise RuntimeError(f"repo_path ঠিক নেই বা ডিরেক্টরি খুঁজে পাওয়া যায়নি: {self.repo_path}")

        self.default_branch = raw.get("default_branch", "main")

        pipeline = raw.get("pipeline", {})
        self.max_retries_per_file = pipeline.get("max_retries_per_file", 3)
        self.max_total_files = pipeline.get("max_total_files", 5)
        self.full_rewrite_line_threshold = pipeline.get("full_rewrite_line_threshold", 150)
        self.require_push_confirmation = pipeline.get("require_push_confirmation", True)

        models = raw.get("models", {})
        self.model_light = models.get("light", "gemini-1.5-flash")
        self.model_strong = models.get("strong", "gemini-1.5-pro")
        self.model_escalation = models.get("escalation", "gemini-1.5-pro")

        self.ignore_dirs = set(raw.get("ignore_dirs", []))
        self.blocklist_files = raw.get("blocklist_files", [])
        self.syntax_checkers = raw.get("syntax_checkers", {})
        self.import_patterns = raw.get("import_patterns", {})

        self.db_path = os.environ.get("AI_EDITOR_DB", os.path.join(_HERE, "runs.db"))

    def next_gemini_key(self):
        return next(self._key_cycle)


CFG = Config(_CONFIG_PATH)
