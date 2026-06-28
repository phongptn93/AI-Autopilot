namespace AdoAutopilot.Models;

public class TenantConfig
{
    public string Name { get; set; } = string.Empty;
    public string AdoOrganization { get; set; } = string.Empty;
    public string AdoProject { get; set; } = string.Empty;
    public string AdoPat { get; set; } = string.Empty;
    public string TriggerTag { get; set; } = "autopilot";
    public string ProcessedTag { get; set; } = "autopilot-done";
    public string RepoWorkingDirectory { get; set; } = string.Empty;
    public string BaseBranch { get; set; } = "development";
    public List<RepoConfig> Repos { get; set; } = new();
    public string TeamsWebhookUrl { get; set; } = string.Empty;

    // RBAC
    public List<string> AllowedUsers { get; set; } = new();
    public List<string> ApproverUsers { get; set; } = new();
    public List<string> AllowedSkills { get; set; } = new();
}
