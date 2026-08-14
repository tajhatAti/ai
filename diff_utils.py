"""
তিনটা কাজ এখানে:
  1. syntax_check       - ভাষা-নির্দিষ্ট deterministic syntax checker
  2. apply_unified_diff - বড় ফাইলে AI-generated unified diff apply করা (fallback সহ)
  3. find_related_files - import/require regex দিয়ে সংশ্লিষ্ট ফাইল খুঁজে বের করা
                           (multi-file feature-এর জন্য context বাড়ানোর কাজে লাগে)
"""

import json
import os
import re
import shutil
import subprocess

from config import CFG


# --------------------------------------------------------------------------
# Syntax check
# --------------------------------------------------------------------------

def syntax_check(rel_path, content):
    ext = os.path.splitext(rel_path)[1].lower()
    full_path = os.path.join(CFG.repo_path, rel_path)
    tmp_path = full_path + ".ai_check_tmp" + ext

    if ext == ".json":
        try:
            json.loads(content)
            return True, "ok"
        except Exception as e:
            return False, f"Invalid JSON: {e}"

    cmd_prefix = CFG.syntax_checkers.get(ext)
    if not cmd_prefix or not shutil.which(cmd_prefix[0]):
        return True, "skipped (no deterministic checker available for this file type)"

    try:
        os.makedirs(os.path.dirname(tmp_path), exist_ok=True)
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(content)
        result = subprocess.run(cmd_prefix + [tmp_path], capture_output=True, text=True)
        ok = result.returncode == 0
        msg = (result.stdout + result.stderr).strip() or "ok"
        return ok, msg
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


# --------------------------------------------------------------------------
# Diff apply (বড় ফাইলে ব্যবহারের জন্য)
# --------------------------------------------------------------------------

def apply_unified_diff(rel_path, diff_text):
    """`patch` কমান্ড দিয়ে diff apply করার চেষ্টা করে। সফল হলে (True, new_content),
    ব্যর্থ হলে (False, error_message)।"""
    if not shutil.which("patch"):
        return False, "patch tool not installed on this system"

    full_path = os.path.join(CFG.repo_path, rel_path)
    diff_file = full_path + ".ai_diff_tmp"
    backup_path = full_path + ".ai_backup_tmp"

    try:
        shutil.copyfile(full_path, backup_path)
        with open(diff_file, "w", encoding="utf-8") as f:
            f.write(diff_text if diff_text.endswith("\n") else diff_text + "\n")

        result = subprocess.run(
            ["patch", "--fuzz=3", "-p0", full_path, diff_file],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            # ব্যর্থ হলে original ফাইল ফিরিয়ে দাও
            shutil.copyfile(backup_path, full_path)
            return False, (result.stdout + result.stderr).strip()

        with open(full_path, "r", encoding="utf-8") as f:
            new_content = f.read()
        return True, new_content
    finally:
        for p in (diff_file, backup_path):
            if os.path.exists(p):
                os.remove(p)


# --------------------------------------------------------------------------
# Dependency-context: সংশ্লিষ্ট ফাইল খুঁজে বের করা
# --------------------------------------------------------------------------

def find_related_files(rel_path, content, all_files):
    """rel_path ফাইলের import/require statement পড়ে repo-র মধ্যে থাকা
    সংশ্লিষ্ট ফাইলগুলোর তালিকা রিটার্ন করে। এতে multi-file feature request-এ
    diagnosis/planning agent শুধু একটা ফাইল না দেখে পুরো ছবিটা দেখতে পারে।"""
    ext = os.path.splitext(rel_path)[1].lower()
    patterns = CFG.import_patterns.get(ext, [])
    if not patterns:
        return []

    raw_refs = set()
    for pat in patterns:
        for m in re.finditer(pat, content):
            raw_refs.add(m.group(1))

    related = set()
    base_dir = os.path.dirname(rel_path)
    for ref in raw_refs:
        # সম্ভাব্য মিল: exact suffix মিল, অথবা basename মিল
        candidates_to_try = {
            ref.lstrip("./"),
            os.path.normpath(os.path.join(base_dir, ref)).replace(os.sep, "/"),
        }
        for f in all_files:
            f_no_ext = os.path.splitext(f)[0]
            for cand in candidates_to_try:
                cand_no_ext = os.path.splitext(cand)[0]
                if f == cand or f_no_ext == cand_no_ext or f.endswith("/" + cand) or os.path.basename(f_no_ext) == os.path.basename(cand_no_ext):
                    related.add(f)

    related.discard(rel_path)
    return sorted(related)[:5]  # অতিরিক্ত context বেড়ে না যাওয়ার জন্য সীমা
