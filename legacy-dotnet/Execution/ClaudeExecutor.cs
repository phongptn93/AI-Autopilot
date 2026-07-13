using System.Diagnostics;
using System.Text;
using AdoAutopilot.Models;
using Microsoft.Extensions.Options;

namespace AdoAutopilot.Execution;

/// <summary>
/// Executes Claude Code CLI with the appropriate skill command.
/// Shells out to `claude` with --yes flag for auto-approval.
/// </summary>
public class ClaudeExecutor
{
    private readonly AutopilotConfig _config;
    private readonly AutoReviewer _reviewer;
    private readonly ILogger<ClaudeExecutor> _logger;

    public ClaudeExecutor(IOptions<AutopilotConfig> config, AutoReviewer reviewer, ILogger<ClaudeExecutor> logger)
    {
        _config = config.Value;
        _reviewer = reviewer;
        _logger = logger;
    }

    public Task<ExecutionResult> ExecuteAsync(WorkItemInfo item, string skillCommand, CancellationToken ct = default)
        => ExecuteAsync(item, skillCommand, draftPr: false, ct);

    public async Task<ExecutionResult> ExecuteAsync(WorkItemInfo item, string skillCommand, bool draftPr, CancellationToken ct = default)
    {
        var sw = Stopwatch.StartNew();
        var branchName = GenerateBranchName(item);

        // Select repo by category (multi-repo support)
        var (workDir, baseBranch) = ResolveRepo(item);

        try
        {
            // Step 1: Create feature branch
            _logger.LogInformation("#{Id} Creating branch: {Branch} in {Repo}", item.Id, branchName, workDir);
            await RunGitAsync($"checkout -b {branchName} origin/{baseBranch}", workDir, ct);

            // Step 2: Execute claude CLI with the skill
            _logger.LogInformation("#{Id} Executing: claude {Skill}", item.Id, skillCommand);
            var claudeOutput = await RunClaudeAsync(skillCommand, workDir, ct);

            // Step 3: Check if there are changes
            var hasChanges = await HasGitChangesAsync(workDir, ct);
            if (!hasChanges)
            {
                _logger.LogWarning("#{Id} No file changes after execution", item.Id);
                await RunGitAsync($"checkout {baseBranch}", workDir, ct);
                return ExecutionResult.Fail(item.Id, skillCommand, "No file changes produced");
            }

            // Step 4: Get changed files
            var changedFiles = await GetChangedFilesAsync(workDir, ct);

            // Step 5: Commit changes
            var commitMsg = $"feat(autopilot): {item.Title} (#{item.Id})";
            await RunGitAsync("add -A", workDir, ct);
            await RunGitAsync($"commit -m \"{EscapeQuotes(commitMsg)}\"", workDir, ct);

            // Step 6: Push branch
            await RunGitAsync($"push -u origin {branchName}", workDir, ct);

            // Step 7: Auto-review before PR
            var review = await _reviewer.ReviewAsync(workDir, ct);
            if (!review.Passed)
            {
                _logger.LogWarning("#{Id} Auto-review blocked PR: {Count} critical issues", item.Id, review.CriticalIssues.Count);
                var failResult = ExecutionResult.Fail(item.Id, skillCommand,
                    $"Auto-review blocked: {string.Join("; ", review.CriticalIssues.Take(3))}");
                failResult.BranchName = branchName;
                failResult.FilesChanged = changedFiles;
                failResult.Duration = sw.Elapsed;
                return failResult;
            }

            // Step 8: Create PR via claude skill
            var prFlag = draftPr ? " --draft" : "";
            _logger.LogInformation("#{Id} Creating PR{Draft}...", item.Id, draftPr ? " (draft)" : "");
            var prOutput = await RunClaudeAsync($"/pr-create{prFlag}", workDir, ct);

            sw.Stop();

            var result = ExecutionResult.Ok(item.Id, skillCommand, claudeOutput);
            result.BranchName = branchName;
            result.FilesChanged = changedFiles;
            result.Duration = sw.Elapsed;

            // Try extract PR URL from output
            var prUrl = ExtractPrUrl(prOutput);
            if (prUrl != null) result.PrUrl = prUrl;

            return result;
        }
        catch (TimeoutException)
        {
            sw.Stop();
            _logger.LogError("#{Id} Execution timed out after {Minutes}m", item.Id, _config.TaskTimeoutMinutes);
            return ExecutionResult.Fail(item.Id, skillCommand, $"Timed out after {_config.TaskTimeoutMinutes} minutes");
        }
        catch (Exception ex)
        {
            sw.Stop();
            _logger.LogError(ex, "#{Id} Execution failed", item.Id);

            // Try to cleanup: switch back to base branch
            try { await RunGitAsync($"checkout {baseBranch}", workDir, ct); } catch { /* best effort */ }

            var result = ExecutionResult.Fail(item.Id, skillCommand, ex.Message);
            result.Duration = sw.Elapsed;
            return result;
        }
    }

    private async Task<string> RunClaudeAsync(string prompt, string workDir, CancellationToken ct)
    {
        var timeout = TimeSpan.FromMinutes(_config.TaskTimeoutMinutes);
        using var cts = CancellationTokenSource.CreateLinkedTokenSource(ct);
        cts.CancelAfter(timeout);

        var psi = new ProcessStartInfo
        {
            FileName = _config.ClaudeCliPath,
            Arguments = $"--yes -p \"{EscapeQuotes(prompt)}\"",
            WorkingDirectory = workDir,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            UseShellExecute = false,
            CreateNoWindow = true
        };

        return await RunProcessAsync(psi, cts.Token);
    }

    private async Task<string> RunGitAsync(string args, string workDir, CancellationToken ct)
    {
        var psi = new ProcessStartInfo
        {
            FileName = "git",
            Arguments = args,
            WorkingDirectory = workDir,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            UseShellExecute = false,
            CreateNoWindow = true
        };

        return await RunProcessAsync(psi, ct);
    }

    private async Task<string> RunProcessAsync(ProcessStartInfo psi, CancellationToken ct)
    {
        using var process = new Process { StartInfo = psi };
        var stdout = new StringBuilder();
        var stderr = new StringBuilder();

        process.OutputDataReceived += (_, e) => { if (e.Data != null) stdout.AppendLine(e.Data); };
        process.ErrorDataReceived += (_, e) => { if (e.Data != null) stderr.AppendLine(e.Data); };

        process.Start();
        process.BeginOutputReadLine();
        process.BeginErrorReadLine();

        await process.WaitForExitAsync(ct);

        if (process.ExitCode != 0)
        {
            var error = stderr.Length > 0 ? stderr.ToString() : stdout.ToString();
            _logger.LogWarning("Process exited with code {Code}: {Cmd} {Args}\n{Error}",
                process.ExitCode, psi.FileName, psi.Arguments, error);
        }

        return stdout.ToString();
    }

    private async Task<bool> HasGitChangesAsync(string workDir, CancellationToken ct)
    {
        var status = await RunGitAsync("status --porcelain", workDir, ct);
        return !string.IsNullOrWhiteSpace(status);
    }

    private async Task<List<string>> GetChangedFilesAsync(string workDir, CancellationToken ct)
    {
        var output = await RunGitAsync("status --porcelain", workDir, ct);
        return output.Split('\n', StringSplitOptions.RemoveEmptyEntries)
            .Select(line => line.Length > 3 ? line[3..].Trim() : line.Trim())
            .Where(f => !string.IsNullOrEmpty(f))
            .ToList();
    }

    private (string workDir, string baseBranch) ResolveRepo(WorkItemInfo item)
    {
        if (_config.Repos.Count > 0)
        {
            var categoryStr = item.Category.ToString();
            var match = _config.Repos.FirstOrDefault(r =>
                r.Categories.Any(c => c.Equals(categoryStr, StringComparison.OrdinalIgnoreCase)));
            if (match != null)
                return (match.Path, match.BaseBranch);
        }
        return (_config.RepoWorkingDirectory, _config.BaseBranch);
    }

    private static string GenerateBranchName(WorkItemInfo item)
    {
        var prefix = item.Category switch
        {
            TaskCategory.Bug => "fix",
            TaskCategory.FrontendTask => "feature/fe",
            TaskCategory.BackendTask => "feature/be",
            _ => "feature"
        };

        var slug = Slugify(item.Title);
        return $"{prefix}/{item.Id}-{slug}";
    }

    private static string Slugify(string text)
    {
        return new string(text.ToLowerInvariant()
            .Where(c => char.IsLetterOrDigit(c) || c == ' ' || c == '-')
            .ToArray())
            .Replace(' ', '-')
            .Trim('-');
    }

    private static string EscapeQuotes(string text) => text.Replace("\"", "\\\"");

    private static string? ExtractPrUrl(string output)
    {
        // Try to find a PR URL in the output
        var lines = output.Split('\n');
        foreach (var line in lines)
        {
            var idx = line.IndexOf("https://dev.azure.com/", StringComparison.OrdinalIgnoreCase);
            if (idx < 0) idx = line.IndexOf("https://", StringComparison.OrdinalIgnoreCase);
            if (idx >= 0 && line.Contains("pullrequest", StringComparison.OrdinalIgnoreCase))
            {
                var end = line.IndexOfAny(new[] { ' ', '\t', '\r', '\n' }, idx);
                return end > idx ? line[idx..end] : line[idx..];
            }
        }
        return null;
    }
}
