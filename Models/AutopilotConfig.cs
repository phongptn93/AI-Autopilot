namespace AdoAutopilot.Models;

public class AutopilotConfig
{
    public const string SectionName = "Autopilot";

    /// <summary>ADO organization URL, e.g. https://dev.azure.com/newoceanis</summary>
    public string AdoOrganization { get; set; } = string.Empty;

    /// <summary>ADO project for work item tracking (e.g. "Khatoco")</summary>
    public string AdoProject { get; set; } = string.Empty;

    /// <summary>ADO Personal Access Token (optional if using OAuth)</summary>
    public string AdoPat { get; set; } = string.Empty;

    /// <summary>OAuth App ID from https://app.vssps.visualstudio.com/app/register</summary>
    public string OAuthAppId { get; set; } = string.Empty;

    /// <summary>OAuth App Secret (client secret)</summary>
    public string OAuthAppSecret { get; set; } = string.Empty;

    /// <summary>Tag on work item that triggers autopilot (e.g. "autopilot")</summary>
    public string TriggerTag { get; set; } = "autopilot";

    /// <summary>Tag added after processing to prevent re-processing</summary>
    public string ProcessedTag { get; set; } = "autopilot-done";

    /// <summary>Poll interval in seconds</summary>
    public int PollIntervalSeconds { get; set; } = 30;

    /// <summary>Working directory where the source repo is cloned</summary>
    public string RepoWorkingDirectory { get; set; } = string.Empty;

    /// <summary>Path to claude CLI executable</summary>
    public string ClaudeCliPath { get; set; } = "claude";

    /// <summary>Max concurrent executions</summary>
    public int MaxConcurrent { get; set; } = 1;

    /// <summary>Timeout per task execution in minutes</summary>
    public int TaskTimeoutMinutes { get; set; } = 30;

    /// <summary>Enable dry-run mode (no actual execution, just logging)</summary>
    public bool DryRun { get; set; } = false;

    /// <summary>Git base branch for new feature branches</summary>
    public string BaseBranch { get; set; } = "development";

    /// <summary>Multi-repo config (overrides RepoWorkingDirectory when set)</summary>
    public List<RepoConfig> Repos { get; set; } = new();

    // ── Retry & Recovery ──

    /// <summary>Max retry attempts per work item (default: 3)</summary>
    public int MaxRetries { get; set; } = 3;

    /// <summary>Base backoff in seconds between retries (exponential: base * 2^n)</summary>
    public int RetryBackoffSeconds { get; set; } = 60;

    // ── Approval Gate ──

    /// <summary>Require human approval (draft PR) before merge (default: true)</summary>
    public bool RequireApproval { get; set; } = true;

    /// <summary>Approval timeout in minutes before auto-escalation (default: 120)</summary>
    public int ApprovalTimeoutMinutes { get; set; } = 120;

    /// <summary>Tag added when PR is created and awaiting review</summary>
    public string ReviewTag { get; set; } = "autopilot-review";

    // ── RBAC ──

    /// <summary>Allowed users (by display name, empty = all allowed)</summary>
    public List<string> AllowedUsers { get; set; } = new();

    /// <summary>Users allowed to approve PRs (empty = all allowed)</summary>
    public List<string> ApproverUsers { get; set; } = new();

    /// <summary>Allowed skill commands (empty = all allowed)</summary>
    public List<string> AllowedSkills { get; set; } = new();

    // ── Multi-tenant ──

    /// <summary>Tenant configurations (empty = single-tenant from main config)</summary>
    public List<TenantConfig> Tenants { get; set; } = new();

    // ── Plugins ──

    /// <summary>Directory to scan for plugin DLLs</summary>
    public string PluginsDirectory { get; set; } = "plugins";

    // ── Cost Tracking ──

    /// <summary>Daily token budget (0 = unlimited)</summary>
    public long DailyBudgetTokens { get; set; } = 0;

    /// <summary>Enable cost alert notifications</summary>
    public bool CostAlertEnabled { get; set; } = false;

    // ── Decomposition ──

    /// <summary>Auto-decompose Requirements into child tasks</summary>
    public bool AutoDecompose { get; set; } = true;

    // ── Feedback Loop ──

    /// <summary>Enable PR feedback loop (auto-fix on rejection)</summary>
    public bool FeedbackLoopEnabled { get; set; } = false;

    /// <summary>Max revision attempts per work item</summary>
    public int MaxRevisions { get; set; } = 3;

    // ── Auto-Review ──

    /// <summary>Enable automated PR review before creating PR (default: true)</summary>
    public bool AutoReviewEnabled { get; set; } = true;

    /// <summary>Block PR creation if these severities found (comma-separated)</summary>
    public string BlockOnSeverity { get; set; } = "Critical,High";

    // ── Scheduling ──

    /// <summary>Schedule window start time (HH:mm, empty = no schedule)</summary>
    public string ScheduleStart { get; set; } = string.Empty;

    /// <summary>Schedule window end time (HH:mm)</summary>
    public string ScheduleEnd { get; set; } = string.Empty;

    /// <summary>Allowed days of week (comma-separated, e.g. "Mon,Tue,Wed,Thu,Fri")</summary>
    public string ScheduleDays { get; set; } = "Mon,Tue,Wed,Thu,Fri";

    // ── Health Check ──

    /// <summary>Port for health check HTTP endpoint (default: 5080)</summary>
    public int HealthPort { get; set; } = 5080;

    // ── Notifications ──

    /// <summary>MS Teams Incoming Webhook URL (leave empty to disable)</summary>
    public string TeamsWebhookUrl { get; set; } = string.Empty;

    /// <summary>Zalo OA Access Token (leave empty to disable)</summary>
    public string ZaloOaAccessToken { get; set; } = string.Empty;

    /// <summary>Zalo recipient user ID to receive notifications</summary>
    public string ZaloRecipientUserId { get; set; } = string.Empty;

    // ── Email (SMTP) ──

    /// <summary>SMTP server host (empty = disabled)</summary>
    public string SmtpHost { get; set; } = string.Empty;

    /// <summary>SMTP server port (default: 587)</summary>
    public int SmtpPort { get; set; } = 587;

    /// <summary>SMTP username</summary>
    public string SmtpUser { get; set; } = string.Empty;

    /// <summary>SMTP password</summary>
    public string SmtpPassword { get; set; } = string.Empty;

    /// <summary>Email recipient address</summary>
    public string EmailTo { get; set; } = string.Empty;

    /// <summary>Email sender address</summary>
    public string EmailFrom { get; set; } = string.Empty;
}
