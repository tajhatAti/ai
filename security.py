"""
পুশ করার আগে বাধ্যতামূলক নিরাপত্তা চেক:
  1. ফাইলের নাম blocklist প্যাটার্নের সাথে মিলছে কিনা (.env, *.pem, secrets ইত্যাদি)
  2. ফাইলের কনটেন্টে hardcoded secret (API key, private key, token) ঢুকে গেছে কিনা

দুটোর যেকোনো একটা ধরা পড়লে পুরো push বন্ধ হয়ে যায় — এটা এড়িয়ে যাওয়ার
কোনো ফ্ল্যাগ ইচ্ছাকৃতভাবেই রাখা হয়নি।
"""

import fnmatch
import re

from config import CFG

# সাধারণ secret প্যাটার্নগুলো (aggressive হলেও ঠিক আছে — false positive হলে
# মানুষ manually দেখে নেবে, কিন্তু leak হয়ে যাওয়া অনেক বেশি খারাপ)
SECRET_PATTERNS = [
    (re.compile(r"AKIA[0-9A-Z]{16}"), "AWS Access Key"),
    (re.compile(r"-----BEGIN (RSA |EC |OPENSSH |)PRIVATE KEY-----"), "Private Key"),
    (re.compile(r"ghp_[A-Za-z0-9]{36}"), "GitHub Personal Access Token"),
    (re.compile(r"AIza[0-9A-Za-z\-_]{35}"), "Google API Key"),
    (re.compile(r"sk-[A-Za-z0-9]{20,}"), "Generic Secret Key (sk-...)"),
    (re.compile(r"(?i)(api[_-]?key|secret|token|password)\s*[:=]\s*[\"'][^\"'\s]{8,}[\"']"), "Hardcoded credential"),
]


def is_blocklisted(rel_path):
    """ফাইলের পাথ যদি blocklist প্যাটার্নের সাথে মেলে, True রিটার্ন করে।"""
    for pattern in CFG.blocklist_files:
        if fnmatch.fnmatch(rel_path, pattern) or fnmatch.fnmatch("/" + rel_path, pattern):
            return True
    return False


def scan_for_secrets(content):
    """কনটেন্টে secret-এর মতো কিছু পেলে (pattern_name, matched_snippet) এর লিস্ট রিটার্ন করে।"""
    findings = []
    for pattern, name in SECRET_PATTERNS:
        for match in pattern.finditer(content):
            snippet = match.group(0)
            # পুরো secret টা লগে না রেখে আংশিক দেখানো
            redacted = snippet[:6] + "..." if len(snippet) > 10 else "***"
            findings.append((name, redacted))
    return findings


def filter_blocklisted_files(file_list):
    """locator স্টেজে দেওয়ার আগে blocklist করা ফাইল বাদ দেয়।"""
    return [f for f in file_list if not is_blocklisted(f)]
