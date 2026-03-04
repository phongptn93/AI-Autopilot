using AdoAutopilot.Models;
using Microsoft.Extensions.Options;

namespace AdoAutopilot.MultiTenant;

public class TenantManager
{
    private readonly AutopilotConfig _config;
    private readonly ILogger<TenantManager> _logger;

    public TenantManager(IOptions<AutopilotConfig> config, ILogger<TenantManager> logger)
    {
        _config = config.Value;
        _logger = logger;
    }

    public List<TenantConfig> GetTenants()
    {
        if (_config.Tenants.Count > 0)
        {
            _logger.LogInformation("Multi-tenant mode: {Count} tenants configured", _config.Tenants.Count);
            return _config.Tenants;
        }

        // Single-tenant fallback: create virtual tenant from main config
        return new List<TenantConfig>
        {
            new()
            {
                Name = _config.AdoProject,
                AdoOrganization = _config.AdoOrganization,
                AdoProject = _config.AdoProject,
                AdoPat = _config.AdoPat,
                TriggerTag = _config.TriggerTag,
                ProcessedTag = _config.ProcessedTag,
                RepoWorkingDirectory = _config.RepoWorkingDirectory,
                BaseBranch = _config.BaseBranch,
                Repos = _config.Repos,
                TeamsWebhookUrl = _config.TeamsWebhookUrl
            }
        };
    }

    public TenantContext GetContext(TenantConfig tenant) => new(tenant);
}
