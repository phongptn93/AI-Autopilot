using AdoAutopilot.Models;

namespace AdoAutopilot.MultiTenant;

public class TenantContext
{
    public TenantConfig Tenant { get; }

    public TenantContext(TenantConfig tenant) => Tenant = tenant;

    public string Name => Tenant.Name;
    public string Organization => Tenant.AdoOrganization;
    public string Project => Tenant.AdoProject;
}
