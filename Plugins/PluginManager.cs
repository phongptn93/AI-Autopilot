using AdoAutopilot.Models;

namespace AdoAutopilot.Plugins;

public class PluginManager
{
    private readonly List<IPlugin> _plugins = new();
    private readonly ILogger<PluginManager> _logger;

    public IReadOnlyList<IPlugin> Plugins => _plugins;
    public IEnumerable<IPreProcessor> PreProcessors => _plugins.OfType<IPreProcessor>();
    public IEnumerable<IPostProcessor> PostProcessors => _plugins.OfType<IPostProcessor>();
    public IEnumerable<ISkillProvider> SkillProviders => _plugins.OfType<ISkillProvider>();

    public PluginManager(ILogger<PluginManager> logger) => _logger = logger;

    public async Task LoadAndInitAsync(string pluginsDirectory, IServiceProvider services)
    {
        var loader = new PluginLoader(services.GetRequiredService<ILogger<PluginLoader>>());
        var loaded = loader.LoadPlugins(pluginsDirectory);

        foreach (var plugin in loaded)
        {
            try
            {
                await plugin.InitializeAsync(services);
                _plugins.Add(plugin);
            }
            catch (Exception ex)
            {
                _logger.LogWarning(ex, "Plugin {Name} failed to initialize", plugin.Name);
            }
        }

        _logger.LogInformation("Plugin manager: {Count} plugins loaded", _plugins.Count);
    }

    public async Task<WorkItemInfo> RunPreProcessorsAsync(WorkItemInfo item, CancellationToken ct)
    {
        foreach (var pp in PreProcessors)
        {
            try { item = await pp.PreProcessAsync(item, ct); }
            catch (Exception ex) { _logger.LogWarning(ex, "PreProcessor {Name} failed", pp.Name); }
        }
        return item;
    }

    public async Task RunPostProcessorsAsync(WorkItemInfo item, ExecutionResult result, CancellationToken ct)
    {
        foreach (var pp in PostProcessors)
        {
            try { await pp.PostProcessAsync(item, result, ct); }
            catch (Exception ex) { _logger.LogWarning(ex, "PostProcessor {Name} failed", pp.Name); }
        }
    }
}
