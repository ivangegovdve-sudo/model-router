package modelrouter.app

data class GoalTemplate(val id: String, val name: String, val description: String, val defaults: ExplainForm)

val GoalTemplates = listOf(
    GoalTemplate("code-review", "Code review", "Review a change for risks and edge cases.", ExplainForm(prompt = "Review this code change for correctness, security and edge cases.", lane = "background", maxTokens = "1024", modelTypes = listOf("text", "reasoning"), thinkingLevel = "high", maxUsdPerMtok = "2.00")),
    GoalTemplate("quick-answer", "Quick answer", "Give a short, direct answer.", ExplainForm(prompt = "Give a short, direct answer to my question.", maxTokens = "256", modelTypes = listOf("text"), maxUsdPerMtok = "0.50")),
    GoalTemplate("research", "Research & analysis", "Compare evidence and trade-offs.", ExplainForm(prompt = "Compare the evidence and explain the trade-offs.", lane = "background", maxTokens = "2048", modelTypes = listOf("text", "reasoning"), thinkingLevel = "high", maxUsdPerMtok = "2.00")),
    GoalTemplate("writing", "Writing & editing", "Improve a draft's clarity.", ExplainForm(prompt = "Improve this draft's clarity while preserving its meaning.", maxTokens = "1024", modelTypes = listOf("text"), maxUsdPerMtok = "1.00")),
    GoalTemplate("tools", "Tool-assisted work", "Plan work that needs tool calls.", ExplainForm(prompt = "Plan this task and use the available tools when needed.", lane = "background", maxTokens = "1024", modelTypes = listOf("text", "reasoning"), toolCalls = true, maxUsdPerMtok = "2.00")),
    GoalTemplate("custom", "Custom", "Choose your own requirements.", ExplainForm()),
)

/** Goal changes fill untouched controls. User text and explicit edits always win. */
fun applyGoalDefaults(current: ExplainForm, goal: GoalTemplate, edited: Set<String> = emptySet()): ExplainForm {
    if (goal.id == "custom") return current
    val d = goal.defaults
    return current.copy(
        prompt = if ("prompt" !in edited && (current.prompt.isBlank() || GoalTemplates.any { it.defaults.prompt == current.prompt })) d.prompt else current.prompt,
        lane = if ("lane" in edited) current.lane else d.lane,
        maxTokens = if ("maxTokens" in edited) current.maxTokens else d.maxTokens,
        filtersEnabled = if ("filtersEnabled" in edited) current.filtersEnabled else d.filtersEnabled,
        providers = if ("providers" in edited) current.providers else d.providers,
        modelTypes = if ("modelTypes" in edited) current.modelTypes else d.modelTypes,
        capabilities = if ("capabilities" in edited) current.capabilities else d.capabilities,
        thinkingLevel = if ("thinkingLevel" in edited) current.thinkingLevel else d.thinkingLevel,
        editable = if ("editable" in edited) current.editable else d.editable,
        toolCalls = if ("toolCalls" in edited) current.toolCalls else d.toolCalls,
        minContextTokens = if ("minContextTokens" in edited) current.minContextTokens else d.minContextTokens,
        maxUsdPerMtok = if ("maxUsdPerMtok" in edited) current.maxUsdPerMtok else d.maxUsdPerMtok,
    )
}
