"""The scanner that is always there: pattern rules in pure Python, no binary to install.

Not a replacement for semgrep or gitleaks — a floor. A team that has installed nothing
still gets the findings that matter most and are cheapest to be sure of: a private key
or cloud credential in the tree, a SQL string built by concatenation, ``innerHTML``
fed from a variable, ``shell=True``. Each rule is a regex with a CWE and an OWASP
category, and each carries the confidence it deserves: a PEM header is ``high``, a
variable called ``password`` assigned a literal is ``medium`` because test fixtures
exist.

Rules are scoped by file extension so the C# rules never run on Python and the noise
stays low. The walk skips what a scanner has no business reading: dependencies, build
output, VCS internals, binaries, minified bundles.
"""

from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass
from pathlib import Path

from ai_autopilot.reports import Finding
from ai_autopilot.security_scan.tools.base import ToolRun, ToolStatus, clip, rel

_MAX_FILE_BYTES = 1_500_000
_MAX_LINE_CHARS = 2000            # a longer line is a minified bundle, not source
_MAX_FINDINGS_PER_RULE_PER_FILE = 5

_SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "bin", "obj", "dist", "build", "out",
    ".venv", "venv", "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    "vendor", "packages", ".angular", ".next", "coverage", "TestResults", ".idea",
    ".vs", "wwwroot/lib", ".autopilot", "reports",
}
_SKIP_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg", ".woff", ".woff2", ".ttf", ".eot",
    ".pdf", ".zip", ".gz", ".7z", ".dll", ".exe", ".pdb", ".so", ".dylib", ".class",
    ".jar", ".pyc", ".db", ".sqlite", ".min.js", ".min.css", ".map", ".lock", ".pptx",
    ".docx", ".xlsx", ".mp4", ".mp3", ".bin", ".dat",
}
# Files whose whole purpose is to hold examples: a `.env.example` with `PASSWORD=changeme`
# is documentation, not a leak. Still scanned for real key material (PEM, AWS ids).
_EXAMPLE_FILE = re.compile(r"(\.example|\.sample|\.template|\.dist)$|README|CHANGELOG", re.I)
_TEST_PATH = re.compile(r"(^|/)(tests?|__tests__|spec|specs|fixtures?|mocks?|e2e)(/|$)", re.I)

# Inline suppression — the convention every scanner has, so a reviewed exception lives
# next to the code it excuses (and moves with it) instead of in a list that drifts:
#   `autopilot:ignore[rule-a, rule-b] <reason>` — only those rules
#   `autopilot:ignore <reason>`                  — every rule on that line
#   `nosec` (bandit) · `nosemgrep` · `gitleaks:allow` — honoured too, all rules
# On the finding's own line, or alone on a comment line directly above it.
_INLINE_IGNORE = re.compile(
    r"autopilot:ignore(?:\[([\w\-, ]+)\])?|\bnosec\b|\bnosemgrep\b|gitleaks:allow", re.I,
)
# A line that is only a comment. Deliberately strict — no `'` (VB) or `;` (ini): a code
# line that starts with a string literal (`'SELECT ' + id`) must never be mistaken for
# one and skipped. `#` covers Python/YAML/shell and C# preprocessor lines alike.
_COMMENT_LINE = re.compile(r"^\s*(#|//|/\*|\*\s|\*$|<!--|\{#|--\s)")
# Credentials published in vendor documentation (AWS's AKIAIOSFODNN7EXAMPLE and its
# secret key) and fixture values that say so. gitleaks allowlists the same stopword:
# flagging the example from the AWS docs trains people to ignore the rule.
_DOC_EXAMPLE = re.compile(r"example", re.I)

CS = (".cs", ".cshtml", ".razor")
TS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".html", ".vue")
PY = (".py",)
CFG = (".json", ".yaml", ".yml", ".xml", ".config", ".env", ".ini", ".toml", ".properties",
       ".txt", ".cfg", ".conf", ".ps1", ".sh", ".bat", ".cmd", ".tf", ".tfvars",
       ".pem", ".key", ".crt", ".example", ".sample", ".template", ".dist")
ANY = CS + TS + PY + CFG + (".sql", ".md", ".java", ".go", ".rb", ".php", ".kt", ".swift")


@dataclass(frozen=True)
class Rule:
    id: str
    title: str
    severity: str
    cwe: str
    owasp: str
    pattern: re.Pattern
    exts: tuple[str, ...]
    confidence: str = "medium"
    detail: str = ""
    # Secrets are reported even in example/test files at lower severity; SAST rules in
    # test paths are dropped entirely (a test that concatenates SQL is testing that).
    secret: bool = False
    # A second pattern that, if it matches the same line, cancels the finding — e.g. a
    # parameterised query mention on a line that also concatenates a string.
    unless: re.Pattern | None = None
    # Evaluate ``unless`` over the whole HTML tag the match sits in (which may span
    # lines), not just the line. A formatter that wraps `rel="noopener"` onto the next
    # line must not turn a correct link into a finding.
    unless_in_tag: bool = False


def _r(p: str, flags: int = 0) -> re.Pattern:
    return re.compile(p, flags)


RULES: tuple[Rule, ...] = (
    # ── secrets ─────────────────────────────────────────────────────────────
    Rule("secret-private-key", "Private key material committed", "critical", "CWE-321",
         "A02:2021", _r(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY(?: BLOCK)?-----"),
         ANY, "high", "A private key in the tree is compromised the moment the repo is cloned.",
         secret=True),
    Rule("secret-aws-access-key", "AWS access key id", "critical", "CWE-798", "A07:2021",
         _r(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), ANY, "high", secret=True),
    Rule("secret-anthropic-key", "Anthropic API key", "critical", "CWE-798", "A07:2021",
         _r(r"\bsk-ant-[A-Za-z0-9_\-]{20,}\b"), ANY, "high", secret=True),
    Rule("secret-openai-key", "OpenAI API key", "critical", "CWE-798", "A07:2021",
         _r(r"\bsk-(?:proj-)?[A-Za-z0-9]{32,}\b"), ANY, "medium", secret=True),
    Rule("secret-github-token", "GitHub token", "critical", "CWE-798", "A07:2021",
         _r(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}\b|\bgithub_pat_[A-Za-z0-9_]{60,}\b"),
         ANY, "high", secret=True),
    Rule("secret-slack-token", "Slack token", "high", "CWE-798", "A07:2021",
         _r(r"\bxox[abprs]-[0-9A-Za-z\-]{10,}\b"), ANY, "high", secret=True),
    Rule("secret-google-api-key", "Google API key", "high", "CWE-798", "A07:2021",
         _r(r"\bAIza[0-9A-Za-z_\-]{35}\b"), ANY, "high", secret=True),
    Rule("secret-azure-storage-key", "Azure storage account key in connection string",
         "critical", "CWE-798", "A07:2021",
         _r(r"AccountKey=[A-Za-z0-9+/]{60,}={0,2}"), ANY, "high", secret=True),
    Rule("secret-azure-sas", "Azure SAS token", "high", "CWE-798", "A07:2021",
         _r(r"[?&]sig=[A-Za-z0-9%+/]{30,}={0,2}"), ANY, "medium", secret=True),
    Rule("secret-azure-devops-pat", "Azure DevOps PAT (52-char base32)", "critical",
         "CWE-798", "A07:2021",
         _r(r"(?i)\b(?:pat|token|personal_access_token)\b[^\n]{0,20}[\"']?([a-z2-7]{52})[\"']?"),
         ANY, "medium", secret=True),
    Rule("secret-jwt-literal", "Hard-coded JWT", "medium", "CWE-798", "A07:2021",
         _r(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
         ANY, "medium", "A literal token in source is either a leaked credential or a "
         "fixture — confirm which.", secret=True),
    Rule("secret-connection-string-password", "Database connection string with password",
         "critical", "CWE-798", "A07:2021",
         _r(r"(?i)(?:Server|Data Source|Host)=[^;\"']+;[^\"'\n]*(?:Password|Pwd)="
            r"(?!\s*[\"']?\s*(?:\$\{|%|\{\{|<|\$\(|__))[^;\"'\s]{4,}"),
         ANY, "high", "Rotate the credential and move it to environment/Key Vault.", secret=True),
    Rule("secret-url-credentials", "Credentials embedded in URL", "high", "CWE-798",
         "A07:2021",
         _r(r"(?i)\b[a-z][a-z0-9+.-]*://[^/\s:@\"']{1,64}:(?!\$\{|%|\{\{|<)[^/\s@\"']{4,}@[^\s\"']+"),
         ANY, "medium", secret=True),
    Rule("secret-password-literal", "Password assigned a literal value", "high", "CWE-259",
         "A07:2021",
         _r(r"(?i)\b(?:password|passwd|pwd|secret|api[_-]?key|client[_-]?secret|access[_-]?token)\b\s*[:=]\s*[\"']([^\"'\s]{8,})[\"']"),
         ANY, "medium", "Literal credentials in source end up in every clone and every log.",
         secret=True,
         unless=_r(r"(?i)\$\{|\{\{|%\(|<%|process\.env|os\.environ|Environment\.|"
                   r"GetEnvironmentVariable|placeholder|changeme|your[_-]|example|xxx|\*{3,}|"
                   r"password\s*[:=]\s*[\"']password[\"']")),

    # ── C# / .NET ───────────────────────────────────────────────────────────
    Rule("cs-sql-concat", "SQL built by string concatenation / interpolation", "high",
         "CWE-89", "API8:2023",
         _r(r"(?:FromSqlRaw|ExecuteSqlRaw|SqlQueryRaw|ExecuteSqlRawAsync|new SqlCommand|"
            r"CommandText\s*=)\s*\(?\s*(?:\$\"|\"[^\"]*\"\s*\+|[A-Za-z_]\w*\s*\+\s*\")"),
         CS, "high", "Use parameters (FromSqlInterpolated / SqlParameter); never splice "
         "user input into SQL.",
         unless=_r(r"FromSqlInterpolated|ExecuteSqlInterpolated")),
    Rule("cs-allow-anonymous", "[AllowAnonymous] on an endpoint", "medium", "CWE-306",
         "API2:2023", _r(r"\[\s*AllowAnonymous\s*\]"), CS, "medium",
         "Confirm the endpoint is meant to be public; this is where auth bypasses hide."),
    Rule("cs-jwt-validate-off", "JWT validation switched off", "critical", "CWE-287",
         "API2:2023",
         _r(r"(?:ValidateIssuer|ValidateAudience|ValidateLifetime|ValidateIssuerSigningKey|"
            r"RequireHttpsMetadata|RequireExpirationTime)\s*=\s*false"),
         CS, "high"),
    Rule("cs-cors-any-origin", "CORS allows any origin", "medium", "CWE-942", "A05:2021",
         _r(r"AllowAnyOrigin\s*\(\)"), CS, "high",
         unless=_r(r"//\s*dev|IsDevelopment")),
    Rule("cs-binaryformatter", "BinaryFormatter deserialisation", "critical", "CWE-502",
         "A08:2021", _r(r"\bBinaryFormatter\b|\bNetDataContractSerializer\b|"
                         r"\bLosFormatter\b|\bSoapFormatter\b"), CS, "high"),
    Rule("cs-typenamehandling", "Json.NET TypeNameHandling enables gadget chains",
         "high", "CWE-502", "A08:2021",
         _r(r"TypeNameHandling\s*=\s*TypeNameHandling\.(?:All|Auto|Objects|Arrays)"), CS, "high"),
    Rule("cs-process-start-concat", "Process started with concatenated arguments", "high",
         "CWE-78", "A03:2021",
         _r(r"Process\.Start\s*\([^)]*(?:\+|\$\")"), CS, "medium"),
    Rule("cs-path-combine-user", "Path built from request input (traversal)", "medium",
         "CWE-22", "A01:2021",
         _r(r"Path\.Combine\s*\([^)]*(?:Request\.|model\.|dto\.|input\.|fileName|filename)"),
         CS, "low", unless=_r(r"GetFileName\s*\(")),
    Rule("cs-weak-hash", "Weak hash algorithm", "medium", "CWE-328", "A02:2021",
         _r(r"\b(?:MD5|SHA1)(?:CryptoServiceProvider|Managed)?\.Create\s*\(|new\s+(?:MD5|SHA1)(?:CryptoServiceProvider|Managed)\s*\("),
         CS, "high", unless=_r(r"(?i)checksum|etag|cache")),
    Rule("cs-random-for-secret", "System.Random used for a secret/token", "medium",
         "CWE-338", "A02:2021",
         _r(r"new\s+Random\s*\([^)]*\)[^\n]*(?i:token|otp|password|secret|salt|nonce)|"
            r"(?i:token|otp|password|secret|salt|nonce)[^\n]*new\s+Random\s*\("),
         CS, "medium"),
    Rule("cs-ssl-validation-off", "TLS certificate validation disabled", "critical",
         "CWE-295", "A02:2021",
         _r(r"ServerCertificateCustomValidationCallback\s*=\s*(?:\([^)]*\)\s*=>\s*true|"
            r"HttpClientHandler\.DangerousAcceptAnyServerCertificateValidator)|"
            r"ServerCertificateValidationCallback\s*[+]?=\s*(?:\([^)]*\)\s*=>\s*true|delegate\s*\{\s*return\s+true)"),
         CS, "high"),
    Rule("cs-html-raw", "Unencoded output via Html.Raw", "medium", "CWE-79", "A03:2021",
         _r(r"Html\.Raw\s*\("), CS, "low"),
    Rule("cs-mass-assignment-bind", "Entity bound straight from the request body", "medium",
         "CWE-915", "API3:2023",
         _r(r"\[FromBody\]\s*(?:[A-Z]\w*)?(?:Entity|Model)\b(?!Dto|Request|Input|ViewModel)"),
         CS, "low"),

    # ── TypeScript / Angular / JS ───────────────────────────────────────────
    Rule("ts-bypass-sanitizer", "Angular sanitizer bypassed", "high", "CWE-79", "A03:2021",
         _r(r"bypassSecurityTrust(?:Html|Script|Style|Url|ResourceUrl)\s*\("), TS, "high",
         "Anything reaching this call must be trusted by construction, not by input."),
    Rule("ts-innerhtml-dynamic", "innerHTML assigned from a variable", "medium", "CWE-79",
         "A03:2021",
         _r(r"\.innerHTML\s*=\s*(?![\"'`]\s*[\"'`]?\s*;)[A-Za-z_$][\w$.]*|"
            r"\[innerHTML\]\s*=\s*\"[^\"]+\"|insertAdjacentHTML\s*\(\s*[\"'][^\"']+[\"']\s*,\s*[A-Za-z_$]"),
         TS, "medium",
         unless=_r(r"DomSanitizer|sanitize|SecurityContext")),
    Rule("ts-eval", "eval / Function constructor / string timer", "high", "CWE-95",
         "A03:2021",
         _r(r"\beval\s*\(|new\s+Function\s*\(|set(?:Timeout|Interval)\s*\(\s*[\"'`]"),
         TS, "high"),
    Rule("ts-document-write", "document.write with dynamic content", "medium", "CWE-79",
         "A03:2021", _r(r"document\.write(?:ln)?\s*\(\s*[A-Za-z_$]"), TS, "medium"),
    Rule("ts-token-localstorage", "Auth token kept in localStorage", "medium", "CWE-922",
         "A07:2021",
         _r(r"localStorage\.setItem\s*\(\s*[\"'][^\"']*(?i:token|jwt|auth|secret)[^\"']*[\"']"),
         TS, "medium", "Readable by any script on the origin — XSS becomes account theft."),
    Rule("ts-http-url-concat", "URL built with unencoded user value", "low", "CWE-20",
         "A03:2021",
         _r(r"(?:this\.http|httpClient|http)\.(?:get|delete|post|put|patch)\s*(?:<[^>]*>)?\s*\(\s*`[^`]*\$\{(?!encodeURIComponent)[^}]*\}"),
         TS, "low", unless=_r(r"encodeURIComponent|environment\.|apiUrl|baseUrl")),
    Rule("ts-postmessage-star", "postMessage to any origin", "medium", "CWE-346", "A05:2021",
         _r(r"postMessage\s*\([^)]*,\s*[\"']\*[\"']\s*\)"), TS, "high"),
    Rule("ts-target-blank", "target=_blank without rel=noopener", "low", "CWE-1022",
         "A05:2021", _r(r"target\s*=\s*[\"']_blank[\"']"),
         (".html", ".tsx", ".jsx", ".vue"), "medium",
         detail="The opened page gets window.opener and can navigate this tab "
                "(reverse tabnabbing). Add rel=\"noopener\" (noreferrer implies it).",
         unless=_r(r"\brel\s*=\s*[\"'][^\"']*\bno(?:opener|referrer)\b"), unless_in_tag=True),

    # ── Python ──────────────────────────────────────────────────────────────
    Rule("py-shell-true", "subprocess with shell=True", "high", "CWE-78", "A03:2021",
         _r(r"\b(?:subprocess\.\w+|os\.system|Popen|create_subprocess_shell)\s*\([^)]*shell\s*=\s*True|create_subprocess_shell\s*\("),
         PY, "medium", unless=_r(r"#\s*nosec|shlex\.quote")),
    Rule("py-eval-exec", "eval / exec on data", "high", "CWE-95", "A03:2021",
         _r(r"(?<![\w.])(?<!def )(?:eval|exec)\s*\(\s*(?![\"'])"), PY, "medium",
         unless=_r(r"^\s*def\s+(?:eval|exec)\s*\(")),
    Rule("py-yaml-load", "yaml.load without SafeLoader", "high", "CWE-502", "A08:2021",
         _r(r"\byaml\.load\s*\((?![^)]*Loader\s*=\s*yaml\.(?:Safe|CSafe|Base)Loader)"), PY, "high"),
    Rule("py-pickle", "pickle / marshal deserialisation", "high", "CWE-502", "A08:2021",
         _r(r"\b(?:pickle|cPickle|marshal|dill)\.loads?\s*\("), PY, "medium"),
    Rule("py-sql-format", "SQL built with format / f-string / %", "high", "CWE-89",
         "A03:2021",
         _r(r"(?:execute|executemany|raw|text)\s*\(\s*(?:f[\"']|[\"'][^\"']*(?:SELECT|INSERT|UPDATE|DELETE)[^\"']*[\"']\s*(?:%|\.format\(|\+))",
            re.I),
         PY, "medium"),
    Rule("py-requests-verify-false", "TLS verification disabled", "high", "CWE-295",
         "A02:2021", _r(r"verify\s*=\s*False"), PY, "high"),
    Rule("py-tempfile-mktemp", "insecure tempfile.mktemp", "low", "CWE-377", "A05:2021",
         _r(r"tempfile\.mktemp\s*\("), PY, "high"),
    Rule("py-debug-true", "Flask/Django debug on", "medium", "CWE-489", "A05:2021",
         _r(r"\bDEBUG\s*=\s*True\b|\.run\s*\([^)]*debug\s*=\s*True"), PY, "medium",
         unless=_r(r"(?i)settings_dev|local|test")),
    Rule("py-hashlib-weak", "MD5/SHA1 for security purpose", "low", "CWE-328", "A02:2021",
         _r(r"hashlib\.(?:md5|sha1)\s*\([^)]*\)[^\n]*(?i:password|token|secret)|"
            r"(?i:password|token|secret)[^\n]*hashlib\.(?:md5|sha1)\s*\("),
         PY, "medium"),

    # ── SQL / config ───────────────────────────────────────────────────────
    Rule("sql-xp-cmdshell", "xp_cmdshell enabled or used", "critical", "CWE-78", "A03:2021",
         _r(r"(?i)\bxp_cmdshell\b"), (".sql", ".cs"), "high"),
    Rule("cfg-debug-detailed-errors", "Detailed errors / developer exception page in config",
         "low", "CWE-209", "A05:2021",
         _r(r"(?i)\"DetailedErrors\"\s*:\s*true|customErrors\s+mode\s*=\s*\"Off\""),
         CFG + CS, "medium"),
    Rule("cfg-http-not-https", "Plain-HTTP endpoint in configuration", "low", "CWE-319",
         "A02:2021",
         _r(r"(?i)[\"']?(?:url|endpoint|host|baseurl|api_?url)[\"']?\s*[:=]\s*[\"']http://"
            r"(?!localhost|127\.|0\.0\.0\.0|10\.|192\.168\.|172\.(?:1[6-9]|2\d|3[01])\.|"
            r"host\.docker|\{|\$|%)"),
         CFG, "low"),
)

_ENTROPY_ASSIGN = re.compile(
    r"(?i)\b(?:secret|token|key|password|passwd|credential)s?\b[\w\-. ]{0,30}[:=]\s*"
    r"[\"']([A-Za-z0-9+/=_\-]{24,})[\"']"
)


def _entropy(s: str) -> float:
    if not s:
        return 0.0
    freq: dict[str, int] = {}
    for ch in s:
        freq[ch] = freq.get(ch, 0) + 1
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in freq.values())


def _skip_dir(name: str) -> bool:
    if name in _SKIP_DIRS:
        return True
    # Dot-directories are tooling state — except the few that hold reviewable config.
    return name.startswith(".") and name not in (".github", ".config", ".claude")


def iter_files(repo: str, files: list[str] | None = None):
    """Yield (absolute Path, relative posix path) for what the scanner should read."""
    root = Path(repo)
    if files:
        for f in files:
            p = Path(f) if Path(f).is_absolute() else root / f
            if p.is_file() and p.suffix.lower() not in _SKIP_SUFFIXES:
                yield p, rel(repo, str(p))
        return
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            entries = sorted(d.iterdir(), key=lambda e: e.name)
        except OSError:
            continue
        for e in entries:
            try:
                if e.is_dir():
                    if not _skip_dir(e.name):
                        stack.append(e)
                    continue
                if e.suffix.lower() in _SKIP_SUFFIXES or e.name.endswith((".min.js", ".min.css")):
                    continue
                if e.stat().st_size > _MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            yield e, rel(repo, str(e))


def _rules_for(path: str) -> list[Rule]:
    lower = path.lower()
    name = lower.rsplit("/", 1)[-1]
    # ``.env``, ``.env.local``, ``.env.production`` — dotenv files have no stable suffix.
    if name.startswith(".env") or name in ("dockerfile", "makefile", "id_rsa", "id_ed25519"):
        lower += ".env"
    return [r for r in RULES if lower.endswith(r.exts)]


def _ignored_inline(lines: list[str], i: int, rule_id: str) -> bool:
    """Is ``rule_id`` excused on line ``i`` by an inline marker (same line, or a comment
    line directly above)? A code line above does not count — its marker is its own."""
    candidates = [lines[i]]
    if i > 0 and _COMMENT_LINE.match(lines[i - 1]):
        candidates.append(lines[i - 1])
    for text in candidates:
        for m in _INLINE_IGNORE.finditer(text):
            scoped = m.group(1)
            if not scoped:
                return True
            if rule_id in {r.strip() for r in scoped.split(",")}:
                return True
    return False


def _tag_region(lines: list[str], i: int, m: re.Match) -> str:
    """The HTML tag containing match ``m`` on line ``i`` — from its ``<`` to its ``>``,
    following the tag onto up to three more lines when a formatter wrapped it."""
    line = lines[i]
    start = line.rfind("<", 0, m.start())
    start = max(start, 0)
    text = line[start:] + "\n" + "\n".join(lines[i + 1:i + 4])
    end = text.find(">", m.end() - start)
    return text[: end + 1] if end >= 0 else text


def _secret_token(line: str, rule: Rule) -> str:
    m = rule.pattern.search(line)
    if not m:
        return ""
    return m.group(1) if m.groups() and m.group(1) else m.group(0)


def scan_text(text: str, relpath: str, stats: dict | None = None) -> list[Finding]:
    """Apply every rule that applies to ``relpath`` over ``text``. Pure; unit-testable.

    ``stats`` (optional) counts what was deliberately NOT reported — ``inline_ignored``
    and ``allowlisted`` — so a run can say "3 excused inline" instead of silently
    reporting fewer findings.
    """
    rules = _rules_for(relpath)
    if not rules:
        return []
    stats = stats if stats is not None else {}
    is_example = bool(_EXAMPLE_FILE.search(relpath))
    is_test = bool(_TEST_PATH.search(relpath))
    out: list[Finding] = []
    per_rule: dict[str, int] = {}
    lines = text.splitlines()
    for i, line in enumerate(lines):
        idx = i + 1
        if len(line) > _MAX_LINE_CHARS:
            continue
        stripped = line.strip()
        if not stripped:
            continue
        for rule in rules:
            if per_rule.get(rule.id, 0) >= _MAX_FINDINGS_PER_RULE_PER_FILE:
                continue
            m = rule.pattern.search(line)
            if not m:
                continue
            if rule.unless is not None:
                scope = _tag_region(lines, i, m) if rule.unless_in_tag else line
                if rule.unless.search(scope):
                    continue
            if not rule.secret and is_test:
                continue
            # Code rules describe code: "never call eval() here" in a comment is advice,
            # not a call. Secret rules still fire — a key pasted into a comment is leaked.
            if not rule.secret and _COMMENT_LINE.match(line):
                continue
            if rule.secret and _DOC_EXAMPLE.search(_secret_token(line, rule)):
                stats["allowlisted"] = stats.get("allowlisted", 0) + 1
                continue
            if _ignored_inline(lines, i, rule.id):
                stats["inline_ignored"] = stats.get("inline_ignored", 0) + 1
                continue
            severity, confidence = rule.severity, rule.confidence
            if rule.secret and (is_example or is_test) and rule.confidence != "high":
                # Documented placeholders and fixtures: keep the finding, drop the alarm.
                severity, confidence = "low", "low"
            per_rule[rule.id] = per_rule.get(rule.id, 0) + 1
            out.append(Finding(
                severity=severity, title=rule.title, file=relpath, line=idx,
                detail=rule.detail, tool="builtin", rule_id=rule.id, cwe=rule.cwe,
                owasp=rule.owasp, confidence=confidence, snippet=clip(_redact(stripped, rule)),
            ))
        # Generic high-entropy secret: only when no explicit rule already fired on the line.
        m = _ENTROPY_ASSIGN.search(line)
        already = any(
            f.line == idx and f.tool == "builtin" and f.rule_id.startswith("secret-") for f in out
        )
        if (m and not already and not _DOC_EXAMPLE.search(m.group(1))
                and not _ignored_inline(lines, i, "secret-high-entropy")):
            value = m.group(1)
            if _entropy(value) >= 4.0 and not re.fullmatch(r"[A-Za-z]+|[0-9]+", value):
                sev, conf = ("low", "low") if (is_example or is_test) else ("medium", "low")
                out.append(Finding(
                    severity=sev, title="High-entropy string assigned to a secret-like name",
                    file=relpath, line=idx, tool="builtin", rule_id="secret-high-entropy",
                    cwe="CWE-798", owasp="A07:2021", confidence=conf,
                    detail="Looks like a credential; confirm it is not a live one.",
                    snippet=clip(_mask(stripped, value)),
                ))
    return out


def _mask(line: str, value: str) -> str:
    """Never echo the whole secret back into a report that will be mailed around."""
    if len(value) <= 8:
        return line.replace(value, "****")
    return line.replace(value, value[:4] + "…" + value[-2:])


def _redact(line: str, rule: Rule) -> str:
    if not rule.secret:
        return line
    m = rule.pattern.search(line)
    if not m:
        return line
    token = m.group(1) if m.groups() and m.group(1) else m.group(0)
    return _mask(line, token) if len(token) >= 8 else line


class BuiltinScanner:
    name = "builtin"

    def available(self) -> bool:
        return True

    async def run(self, repo: str, files: list[str] | None = None) -> ToolRun:
        started = time.monotonic()
        findings: list[Finding] = []
        count = 0
        stats: dict[str, int] = {}
        for path, relpath in iter_files(repo, files):
            count += 1
            try:
                raw = path.read_bytes()
            except OSError:
                continue
            if b"\x00" in raw[:4096]:
                continue  # binary
            findings.extend(scan_text(raw.decode("utf-8", errors="replace"), relpath, stats))
        status = ToolStatus(
            self.name, ran=True, duration_seconds=time.monotonic() - started,
            findings=len(findings), version=f"rules:{len(RULES)}",
            extra={"files": count, **stats},
        )
        return ToolRun(findings, status)
