namespace AdoAutopilot.Models;

public class RepoConfig
{
    public string Name { get; set; } = string.Empty;
    public string Path { get; set; } = string.Empty;
    public string BaseBranch { get; set; } = "development";
    public List<string> Categories { get; set; } = new();
}
