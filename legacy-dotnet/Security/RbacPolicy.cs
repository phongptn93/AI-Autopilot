using AdoAutopilot.Models;
using Microsoft.Extensions.Options;

namespace AdoAutopilot.Security;

public class RbacPolicy
{
    private readonly AutopilotConfig _config;
    private readonly ILogger<RbacPolicy> _logger;

    public RbacPolicy(IOptions<AutopilotConfig> config, ILogger<RbacPolicy> logger)
    {
        _config = config.Value;
        _logger = logger;
    }

    public bool IsUserAllowed(WorkItemInfo item)
    {
        if (_config.AllowedUsers.Count == 0) return true; // No restriction

        var createdBy = item.CreatedBy ?? string.Empty;
        var allowed = _config.AllowedUsers.Any(u =>
            createdBy.Contains(u, StringComparison.OrdinalIgnoreCase));

        if (!allowed)
            _logger.LogWarning("RBAC: #{Id} created by '{User}' — not in allowed list",
                item.Id, createdBy);

        return allowed;
    }

    public bool IsSkillAllowed(string skill)
    {
        if (_config.AllowedSkills.Count == 0) return true; // No restriction
        return _config.AllowedSkills.Any(s => skill.Contains(s, StringComparison.OrdinalIgnoreCase));
    }

    public bool IsApprover(string userName)
    {
        if (_config.ApproverUsers.Count == 0) return true; // Everyone can approve
        return _config.ApproverUsers.Any(u =>
            userName.Contains(u, StringComparison.OrdinalIgnoreCase));
    }
}
