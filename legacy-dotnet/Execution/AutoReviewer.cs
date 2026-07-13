using System.Diagnostics;
using System.Text.RegularExpressions;
using AdoAutopilot.Models;
using Microsoft.Extensions.Options;

namespace AdoAutopilot.Execution;

public class ReviewResult
{
    public bool Passed { get; set; }
    public List<string> CriticalIssues { get; set; } = new();
    public List<string> Warnings { get; set; } = new();
    public string RawOutput { get; set; } = string.Empty;
}

public class AutoReviewer
{
    private readonly AutopilotConfig _config;
    private readonly ILogger<AutoReviewer> _logger;

    public AutoReviewer(IOptions<AutopilotConfig> config, ILogger<AutoReviewer> logger)
    {
        _config = config.Value;
        _logger = logger;
    }

    public async Task<ReviewResult> ReviewAsync(string workingDir, CancellationToken ct)
    {
        if (!_config.AutoReviewEnabled) return new ReviewResult { Passed = true };

        var result = new ReviewResult();
        var blockedSeverities = _config.BlockOnSeverity
            .Split(',', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries)
            .Select(s => s.ToUpperInvariant())
            .ToHashSet();

        _logger.LogInformation("Running auto-review on {Dir}", workingDir);

        // Run security review
        var secOutput = await RunClaudeReviewAsync(
            "Review this branch for security issues. List each issue with severity: Critical, High, Medium, or Low.",
            workingDir, ct);
        result.RawOutput += secOutput + "\n";

        // Parse severities from output
        ParseIssues(secOutput, result, blockedSeverities);

        _logger.LogInformation("Auto-review: {Critical} critical, {Warnings} warnings",
            result.CriticalIssues.Count, result.Warnings.Count);

        result.Passed = result.CriticalIssues.Count == 0;
        return result;
    }

    private static void ParseIssues(string output, ReviewResult result, HashSet<string> blockedSeverities)
    {
        var lines = output.Split('\n');
        foreach (var line in lines)
        {
            var upper = line.ToUpperInvariant();
            if (blockedSeverities.Any(s => upper.Contains(s)))
                result.CriticalIssues.Add(line.Trim());
            else if (upper.Contains("MEDIUM") || upper.Contains("LOW"))
                result.Warnings.Add(line.Trim());
        }
    }

    private async Task<string> RunClaudeReviewAsync(string prompt, string workingDir, CancellationToken ct)
    {
        try
        {
            var psi = new ProcessStartInfo
            {
                FileName = _config.ClaudeCliPath,
                Arguments = $"--yes -p \"{prompt.Replace("\"", "\\\"")}\"",
                WorkingDirectory = workingDir,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                UseShellExecute = false,
                CreateNoWindow = true
            };

            using var process = Process.Start(psi);
            if (process == null) return string.Empty;

            using var cts = CancellationTokenSource.CreateLinkedTokenSource(ct);
            cts.CancelAfter(TimeSpan.FromMinutes(5));

            var output = await process.StandardOutput.ReadToEndAsync(cts.Token);
            await process.WaitForExitAsync(cts.Token);
            return output;
        }
        catch (Exception ex)
        {
            _logger.LogWarning(ex, "Auto-review command failed");
            return string.Empty;
        }
    }
}
