using AdoAutopilot.Ado;
using Microsoft.Extensions.Diagnostics.HealthChecks;

namespace AdoAutopilot.HealthChecks;

public class AdoHealthCheck : IHealthCheck
{
    private readonly AdoAuthService _auth;
    private readonly IHttpClientFactory _httpFactory;
    private readonly ILogger<AdoHealthCheck> _logger;

    public AdoHealthCheck(AdoAuthService auth, IHttpClientFactory httpFactory, ILogger<AdoHealthCheck> logger)
    {
        _auth = auth;
        _httpFactory = httpFactory;
        _logger = logger;
    }

    public async Task<HealthCheckResult> CheckHealthAsync(HealthCheckContext context, CancellationToken ct = default)
    {
        try
        {
            var header = await _auth.GetAuthHeaderAsync(ct);
            var http = _httpFactory.CreateClient();
            http.DefaultRequestHeaders.Authorization = header;

            var response = await http.GetAsync("https://dev.azure.com/_apis/projects?$top=1&api-version=7.1", ct);
            if (response.IsSuccessStatusCode)
                return HealthCheckResult.Healthy("ADO API reachable");

            return HealthCheckResult.Degraded($"ADO API returned {response.StatusCode}");
        }
        catch (Exception ex)
        {
            _logger.LogWarning(ex, "ADO health check failed");
            return HealthCheckResult.Unhealthy("ADO API unreachable", ex);
        }
    }
}
