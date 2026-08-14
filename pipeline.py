"""
মূল multi-agent pipeline। প্রতিটা স্টেজ একটা আলাদা ফাংশন — টেস্ট করা ও
বদলানো সহজ রাখার জন্য। orchestrate_prepare() পুরো pipeline চালিয়ে
(locate -> diagnose -> plan -> per-file implement/check/review -> approval)
একটা "ready to push" state তৈরি করে কিন্তু push করে না — push হয়
confirm_and_push() থেকে, যাতে ব্যবহারকারী চাইলে confirm করার সুযোগ পায়।
"""

import json

from config import CFG
from gemini_client import call_gemini, call_gemini_json, strip_code_fences
from git_utils import build_file_list, read_file, write_file, create_new_branch, commit_all, push_branch, discard_branch
from diff_utils import syntax_check, apply_unified_diff, find_related_files
from security import filter_blocklisted_files, scan_for_secrets, is_blocklisted


class PipelineResult:
    def __init__(self):
        self.ok = False
        self.reason = ""
        self.branch = None
        self.diagnosis = ""
        self.target_files = []
        self.final_codes = {}   # rel_path -> new content (RAM-এ, এখনো ফাইলে লেখা হয়নি)
        self.summaries = []
        self.approval_reason = ""


# --------------------------------------------------------------------------
# Stage 1: Locator
# --------------------------------------------------------------------------

def stage_locate_files(user_prompt, file_list):
    safe_list = filter_blocklisted_files(file_list)
    prompt = (
        "You are a code locator agent. Given a project's file list and a user's problem "
        "description, identify up to 3 files that are MOST LIKELY related to the issue.\n\n"
        f"User's request:\n{user_prompt}\n\n"
        f"Project files:\n{chr(10).join(safe_list)}\n\n"
        'Respond ONLY with JSON: {"candidates": ["relative/path1", "relative/path2"]}\n'
        "Paths must exactly match entries from the file list above."
    )
    data = call_gemini_json(prompt, model_name=CFG.model_light, fallback={"candidates": []})
    candidates = [c for c in data.get("candidates", []) if c in safe_list]
    return candidates[:3]


# --------------------------------------------------------------------------
# Stage 2: Diagnosis (+ dependency-context বিস্তৃতি)
# --------------------------------------------------------------------------

def stage_diagnose(user_prompt, candidates, all_files):
    # প্রতিটা candidate-এর সাথে সংশ্লিষ্ট (imported/required) ফাইলও context-এ যোগ করি
    expanded = dict()
    for c in candidates:
        content = read_file(c)
        expanded[c] = content
        for related in find_related_files(c, content, all_files):
            if related not in expanded and not is_blocklisted(related):
                expanded[related] = read_file(related)

    blob = "\n\n".join(f"----- FILE: {path} -----\n{content}" for path, content in expanded.items())
    prompt = (
        "You are a senior software engineer diagnosing a bug/feature request. You are given "
        "the candidate files PLUS files they depend on (imports), so you can see the full picture "
        "when a change needs to touch more than one file.\n\n"
        f"User request:\n{user_prompt}\n\n"
        f"Files:\n{blob}\n\n"
        "Determine the root cause and list ALL files that actually need to change. If the "
        "request needs changes in multiple files (e.g. a shared component + its stylesheet, "
        "or a function + everywhere it's called), include all of them.\n\n"
        'Respond ONLY with JSON: {"diagnosis": "short explanation", "target_files": ["path1", "path2"]}'
    )
    data = call_gemini_json(prompt, model_name=CFG.model_strong, fallback={"diagnosis": "", "target_files": candidates})
    targets = [t for t in data.get("target_files", []) if t in expanded or t in all_files]
    if not targets:
        targets = candidates
    return data.get("diagnosis", ""), targets[: CFG.max_total_files], expanded


# --------------------------------------------------------------------------
# Stage 3: Planning
# --------------------------------------------------------------------------

def stage_plan(user_prompt, diagnosis, target_files):
    prompt = (
        "You are a planning agent splitting a code change across multiple files. Make sure "
        "the per-file instructions stay CONSISTENT with each other (e.g. if a function name "
        "or prop changes in one file, every other file that uses it must be told to update too).\n\n"
        f"User request:\n{user_prompt}\n\n"
        f"Diagnosis:\n{diagnosis}\n\n"
        f"Target files:\n{chr(10).join(target_files)}\n\n"
        'Respond ONLY with JSON: {"tasks": {"path1": "instruction1", "path2": "instruction2"}}'
    )
    data = call_gemini_json(prompt, model_name=CFG.model_strong, fallback={"tasks": {}})
    tasks = data.get("tasks", {})
    for f in target_files:
        tasks.setdefault(f, user_prompt)
    return tasks


# --------------------------------------------------------------------------
# Stage 4: Implementation (ছোট ফাইলে full rewrite, বড় ফাইলে diff চেষ্টা করে fallback সহ)
# --------------------------------------------------------------------------

def _implement_full_rewrite(rel_path, instruction, original_code, feedback):
    feedback_block = f"\n\nPrevious attempt had this problem, fix it:\n{feedback}" if feedback else ""
    prompt = (
        f"You are editing the file `{rel_path}`.\n\n"
        f"Instruction:\n{instruction}{feedback_block}\n\n"
        f"Current full file content:\n-----BEGIN FILE-----\n{original_code}\n-----END FILE-----\n\n"
        "Return the COMPLETE updated file content applying the change. Keep unrelated parts "
        "intact. Reply with ONLY the raw file content, no markdown fences, no commentary."
    )
    raw = call_gemini(prompt, model_name=CFG.model_strong)
    return strip_code_fences(raw)


def _implement_via_diff(rel_path, instruction, original_code, feedback):
    feedback_block = f"\n\nPrevious attempt had this problem, fix it:\n{feedback}" if feedback else ""
    prompt = (
        f"You are editing the file `{rel_path}` (this is a large file, so produce a MINIMAL "
        "unified diff instead of rewriting the whole thing).\n\n"
        f"Instruction:\n{instruction}{feedback_block}\n\n"
        f"Current full file content:\n-----BEGIN FILE-----\n{original_code}\n-----END FILE-----\n\n"
        f"Reply with ONLY a valid unified diff (--- / +++ / @@ headers, paths as {rel_path}), "
        "no markdown fences, no commentary."
    )
    raw = call_gemini(prompt, model_name=CFG.model_strong)
    diff_text = strip_code_fences(raw)
    ok, result = apply_unified_diff(rel_path, diff_text)
    if ok:
        return result
    # diff apply ব্যর্থ হলে full rewrite-এ fallback
    return _implement_full_rewrite(rel_path, instruction, original_code, feedback)


def stage_implement(rel_path, instruction, original_code, feedback=None):
    line_count = original_code.count("\n")
    if line_count > CFG.full_rewrite_line_threshold:
        return _implement_via_diff(rel_path, instruction, original_code, feedback)
    return _implement_full_rewrite(rel_path, instruction, original_code, feedback)


# --------------------------------------------------------------------------
# Stage 5: Dual independent review
# --------------------------------------------------------------------------

def stage_review(rel_path, instruction, original_code, new_code, reviewer_label):
    prompt = (
        f"You are independent code reviewer '{reviewer_label}'. Review this change for bugs, "
        "syntax mistakes, missing pieces, or logic errors. Be strict.\n\n"
        f"File: {rel_path}\n"
        f"Intended change:\n{instruction}\n\n"
        f"ORIGINAL:\n{original_code}\n\n"
        f"UPDATED:\n{new_code}\n\n"
        'Respond ONLY with JSON: {"ok": true or false, "issues": "description or empty string"}'
    )
    data = call_gemini_json(prompt, model_name=CFG.model_strong, fallback={"ok": True, "issues": ""})
    return bool(data.get("ok", True)), data.get("issues", "")


# --------------------------------------------------------------------------
# Stage 6: Final approval
# --------------------------------------------------------------------------

def stage_final_approval(user_prompt, diagnosis, file_summaries):
    prompt = (
        "You are the final approval agent before code gets pushed. Decide if the overall "
        "change set is safe and correct.\n\n"
        f"Original user request:\n{user_prompt}\n\n"
        f"Diagnosis:\n{diagnosis}\n\n"
        f"Per-file summary:\n{json.dumps(file_summaries, ensure_ascii=False, indent=2)}\n\n"
        'Respond ONLY with JSON: {"approve": true or false, "reason": "short reason"}'
    )
    data = call_gemini_json(prompt, model_name=CFG.model_strong, fallback={"approve": False, "reason": "parse error"})
    return bool(data.get("approve", False)), data.get("reason", "")


# --------------------------------------------------------------------------
# একটা ফাইলের জন্য পুরো implement -> check -> review লুপ
# --------------------------------------------------------------------------

def _process_single_file(rel_path, instruction, status_cb):
    original_code = read_file(rel_path)
    feedback = None
    current_code = None

    for attempt in range(1, CFG.max_retries_per_file + 1):
        status_cb(f"⚙️ {rel_path} — এডিট (চেষ্টা {attempt}/{CFG.max_retries_per_file})...")
        current_code = stage_implement(rel_path, instruction, original_code, feedback)

        if not current_code.strip():
            feedback = "Model returned empty content."
            continue

        status_cb(f"🔍 {rel_path} — syntax check...")
        ok, msg = syntax_check(rel_path, current_code)
        if not ok:
            feedback = f"Syntax check failed: {msg}"
            continue

        status_cb(f"🧪 {rel_path} — dual AI review...")
        ok1, issues1 = stage_review(rel_path, instruction, original_code, current_code, "Reviewer-A")
        ok2, issues2 = stage_review(rel_path, instruction, original_code, current_code, "Reviewer-B")

        if ok1 and ok2:
            secrets_found = scan_for_secrets(current_code)
            if secrets_found:
                feedback = "Secret scanner blocked this: " + "; ".join(f"{n} ({s})" for n, s in secrets_found)
                continue
            return True, current_code, {
                "file": rel_path, "attempts": attempt, "syntax": msg, "review": "both reviewers approved",
            }

        feedback = f"Reviewer-A: {issues1 or 'ok'} | Reviewer-B: {issues2 or 'ok'}"

    return False, current_code, {
        "file": rel_path, "attempts": CFG.max_retries_per_file, "syntax": "n/a",
        "review": f"failed after {CFG.max_retries_per_file} tries — last feedback: {feedback}",
    }


# --------------------------------------------------------------------------
# পুরো pipeline: লোকেট থেকে অ্যাপ্রুভাল পর্যন্ত (push বাদে)
# --------------------------------------------------------------------------

def orchestrate_prepare(user_prompt, status_cb):
    result = PipelineResult()

    status_cb("🔍 ফাইল ট্রি বানানো হচ্ছে...")
    all_files = build_file_list()
    if not all_files:
        result.reason = "রিপোতে কোনো ফাইল পাওয়া যায়নি।"
        return result

    status_cb("🧭 কোন ফাইলে সমস্যা হতে পারে খুঁজছে...")
    candidates = stage_locate_files(user_prompt, all_files)
    if not candidates:
        result.reason = "কোনো candidate ফাইল পাওয়া যায়নি (blocklist বাদ দিয়ে)।"
        return result

    status_cb(f"🧠 রুট-কজ + সংশ্লিষ্ট ফাইল analyze করছে ({', '.join(candidates)})...")
    diagnosis, target_files, _ = stage_diagnose(user_prompt, candidates, all_files)
    if not target_files:
        result.reason = "Diagnosis-এ কোনো target file পাওয়া যায়নি।"
        return result
    result.diagnosis = diagnosis
    result.target_files = target_files

    status_cb(f"📋 কাজ ভাগ করা হচ্ছে ({len(target_files)}টা ফাইল: {', '.join(target_files)})...")
    tasks = stage_plan(user_prompt, diagnosis, target_files)

    for rel_path in target_files:
        instruction = tasks.get(rel_path, user_prompt)
        success, code, summary = _process_single_file(rel_path, instruction, status_cb)
        result.summaries.append(summary)
        if success:
            result.final_codes[rel_path] = code
        else:
            result.reason = f"'{rel_path}' ফাইলে {CFG.max_retries_per_file} বার চেষ্টা করেও ঠিক করা যায়নি।"
            return result

    status_cb("🧑‍⚖️ Final approval agent চেক করছে...")
    approved, reason = stage_final_approval(user_prompt, diagnosis, result.summaries)
    result.approval_reason = reason
    if not approved:
        result.reason = f"Final approval agent reject করেছে: {reason}"
        return result

    result.ok = True
    return result


def confirm_and_push(result: PipelineResult, commit_message):
    """orchestrate_prepare()-এর ফলাফল নিয়ে আসল branch/commit/push করে।
    এটা একটা আলাদা ফাংশন যাতে push হওয়ার আগে ব্যবহারকারী confirm করার সুযোগ পায়।"""
    branch = create_new_branch()
    try:
        for rel_path, code in result.final_codes.items():
            write_file(rel_path, code)
        commit_all(list(result.final_codes.keys()), commit_message)
        push_branch(branch)
        return branch
    except Exception:
        discard_branch(branch)
        raise
