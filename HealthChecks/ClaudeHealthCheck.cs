using System.Diagnostics;
using Microsoft.Extensions.Diagnostics.HealthChecks;

namespace AdoAutopilot.HealthChecks;

public class ClaudeHealthCheck : IHealthCheck
{
    private readonly ILogger<ClaudeHealthCheck> _logger;

    public ClaudeHealthCheck(ILogger<ClaudeHealthCheck> logger) => _logger = logger;

    public async Task<HealthCheckResult> CheckHealthAsync(HealthCheckContext context, CancellationToken ct = default)
    {
        try
        {
            var psi = new ProcessStartInfo("claude", "--version")
            {
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                UseShellExecute = false,
                CreateNoWindow = true
            };

            using var process = Process.Start(psi);
            if (process == null)
                return HealthCheckResult.Unhealthy("Failed to start claude process");

            var output = await process.StandardOutput.ReadToEndAsync(ct);
            await process.WaitForExitAsync(ct);

            return process.ExitCode == 0
                ? HealthCheckResult.Healthy($"claude CLI: {output.Trim()}")
                : HealthCheckResult.Degraded($"claude exited with code {process.ExitCode}");
        }
        catch (Exception ex)
        {
            _logger.LogWarning(ex, "Claude health check failed");
            return HealthCheckResult.Unhealthy("claude CLI not available", ex);
        }
    }
}
