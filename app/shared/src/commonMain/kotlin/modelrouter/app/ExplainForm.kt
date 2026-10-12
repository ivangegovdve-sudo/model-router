package modelrouter.app

/** The Explain form only validates input. Qualification and ranking stay in the router. */
data class ExplainForm(
    val prompt: String = "",
    val seat: String? = null,
    val lane: String = "interactive",
    val maxTokens: String = "",
    val includeTool: Boolean = false,
    val filtersEnabled: Boolean = true,
    val providers: List<String> = emptyList(),
    val modelTypes: List<String> = emptyList(),
    val capabilities: List<String> = emptyList(),
    val thinkingLevel: String? = null,
    val editable: Boolean? = null,
    val toolCalls: Boolean? = null,
    val minContextTokens: String = "",
    val maxUsdPerMtok: String = "",
) {
    fun request(): ExplainRequest {
        require(prompt.isNotBlank()) { "Enter request context first." }
        require(lane in listOf("interactive", "background", "batch")) { "Choose a valid routing lane." }
        val budget = maxTokens.trim().let {
            if (it.isEmpty()) null else it.toIntOrNull()?.takeIf { v -> v > 0 }
                ?: throw IllegalArgumentException("Answer budget must be a positive whole number.")
        }
        val requirements = if (filtersEnabled) {
            require(toolCalls != false || !includeTool) { "Tool calls are excluded, but a tool is supplied. Remove the tool or change the filter." }
            require(toolCalls != false || "tools" !in capabilities) { "Tool calls cannot be both required and excluded." }
            require(editable != false || "editing" !in capabilities) { "Editing cannot be both required and excluded." }
            val context = minContextTokens.trim().let {
                if (it.isEmpty()) null else it.toLongOrNull()?.takeIf { v -> v > 0 }
                    ?: throw IllegalArgumentException("Minimum context must be a positive whole number.")
            }
            val ceiling = maxUsdPerMtok.trim().let {
                if (it.isEmpty()) null else it.toDoubleOrNull()?.takeIf { v -> v.isFinite() && v >= 0 }
                    ?: throw IllegalArgumentException("Price ceiling must be a finite number of zero or more.")
            }
            RouterRequirements(
                providers = providers,
                modelTypes = modelTypes,
                requiredCapabilities = capabilities,
                thinkingLevel = thinkingLevel,
                editable = editable,
                toolCalls = toolCalls,
                minContextTokens = context,
                maxUsdPerMtok = ceiling,
            )
        } else null
        return ExplainRequest(
            model = seat ?: "auto:$lane",
            messages = listOf(ExplainMessage(content = prompt)),
            maxTokens = budget,
            routerRequirements = requirements,
            tools = if (includeTool) listOf(RouterJson.parseToJsonElement(
                """{"type":"function","function":{"name":"lookup","description":"Look up a value","parameters":{"type":"object","properties":{"query":{"type":"string"}},"required":["query"]}}}""",
            )) else null,
        )
    }
}
