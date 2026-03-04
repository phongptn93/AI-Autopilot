using Microsoft.Extensions.Diagnostics.HealthChecks;

namespace AdoAutopilot.HealthChecks;

public class DiskSpaceHealthCheck : IHealthCheck
{
    private const long MinFreeBytes = 1L * 1024 * 1024 * 1024; // 1 GB

    public Task<HealthCheckResult> CheckHealthAsync(HealthCheckContext context, CancellationToken ct = default)
    {
        var drive = new DriveInfo(Path.GetPathRoot(AppContext.BaseDirectory) ?? "C");
        var freeGb = drive.AvailableFreeSpace / (1024.0 * 1024 * 1024);
        var data = new Dictionary<string, object>
        {
            ["drive"] = drive.Name,
            ["freeGB"] = Math.Round(freeGb, 2),
            ["totalGB"] = Math.Round(drive.TotalSize / (1024.0 * 1024 * 1024), 2)
        };

        return Task.FromResult(drive.AvailableFreeSpace >= MinFreeBytes
            ? HealthCheckResult.Healthy($"{freeGb:F1} GB free on {drive.Name}", data)
            : HealthCheckResult.Unhealthy($"Only {freeGb:F1} GB free on {drive.Name} (min 1 GB)", data: data));
    }
}
