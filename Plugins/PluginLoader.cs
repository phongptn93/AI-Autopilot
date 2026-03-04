using System.Reflection;

namespace AdoAutopilot.Plugins;

public class PluginLoader
{
    private readonly ILogger<PluginLoader> _logger;

    public PluginLoader(ILogger<PluginLoader> logger) => _logger = logger;

    public List<IPlugin> LoadPlugins(string pluginsDirectory)
    {
        var plugins = new List<IPlugin>();

        if (string.IsNullOrEmpty(pluginsDirectory) || !Directory.Exists(pluginsDirectory))
        {
            _logger.LogDebug("No plugins directory found at {Dir}", pluginsDirectory);
            return plugins;
        }

        var dlls = Directory.GetFiles(pluginsDirectory, "*.dll");
        foreach (var dll in dlls)
        {
            try
            {
                var assembly = Assembly.LoadFrom(dll);
                var pluginTypes = assembly.GetTypes()
                    .Where(t => typeof(IPlugin).IsAssignableFrom(t) && !t.IsAbstract && !t.IsInterface);

                foreach (var type in pluginTypes)
                {
                    if (Activator.CreateInstance(type) is IPlugin plugin)
                    {
                        plugins.Add(plugin);
                        _logger.LogInformation("Loaded plugin: {Name} v{Version} from {Dll}",
                            plugin.Name, plugin.Version, Path.GetFileName(dll));
                    }
                }
            }
            catch (Exception ex)
            {
                _logger.LogWarning(ex, "Failed to load plugin from {Dll}", dll);
            }
        }

        return plugins;
    }
}
