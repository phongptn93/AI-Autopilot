using AdoAutopilot.Models;

namespace AdoAutopilot.Routing;

public static class WorkItemPriority
{
    public static int GetScore(WorkItemInfo item)
    {
        var baseScore = item.Category switch
        {
            TaskCategory.Bug => 60,
            TaskCategory.BackendTask => 40,
            TaskCategory.FrontendTask => 40,
            TaskCategory.TestTask => 35,
            TaskCategory.Requirement => 20,
            TaskCategory.DatabaseTask => 30,
            _ => 10
        };

        // Boost by ADO Priority field (1=Critical → +40, 2=High → +20, 3=Normal → 0, 4=Low → -10)
        baseScore += item.Priority switch
        {
            1 => 40,
            2 => 20,
            3 => 0,
            4 => -10,
            _ => 0
        };

        return baseScore;
    }

    public static List<WorkItemInfo> Sort(List<WorkItemInfo> items)
        => items.OrderByDescending(GetScore).ToList();
}
