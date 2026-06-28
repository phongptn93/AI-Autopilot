using AdoAutopilot.Models;
using AdoAutopilot.Notifications;

namespace AdoAutopilot.Plugins;

public interface IPlugin
{
    string Name { get; }
    string Version { get; }
    Task InitializeAsync(IServiceProvider services);
}

public interface IPreProcessor : IPlugin
{
    Task<WorkItemInfo> PreProcessAsync(WorkItemInfo item, CancellationToken ct);
}

public interface IPostProcessor : IPlugin
{
    Task PostProcessAsync(WorkItemInfo item, ExecutionResult result, CancellationToken ct);
}

public interface ISkillProvider : IPlugin
{
    bool CanHandle(WorkItemInfo item);
    string GetSkillCommand(WorkItemInfo item);
}
