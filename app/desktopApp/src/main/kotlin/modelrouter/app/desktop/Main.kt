package modelrouter.app.desktop

import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.ui.unit.DpSize
import androidx.compose.ui.unit.dp
import androidx.compose.ui.window.Window
import androidx.compose.ui.window.application
import androidx.compose.ui.window.rememberWindowState
import modelrouter.app.DesktopSettingsStore
import modelrouter.app.ui.AppController
import modelrouter.app.ui.ModelRouterApp
import modelrouter.app.ui.Screen
import java.awt.Rectangle
import java.awt.Robot
import java.io.File
import javax.imageio.ImageIO

/**
 * Flags:
 *   --theme dark|light                        override the system scheme
 *   --size 1600x1000                          window size in dp
 *   --shot <file.png>                         capture the real window after it renders, then exit (verification)
 *   --tour "sec=screen:file.png;..."          drive the app to each named screen (decisions|roster|explain|setup)
 *                                              at the given second mark and capture it, then exit
 *   --explain-prompt "text"                   prompt used to exercise the Explain screen during --tour
 */
fun main(args: Array<String>) {
    fun arg(name: String): String? = args.indexOf(name).takeIf { it >= 0 && it + 1 < args.size }?.let { args[it + 1] }

    val store = DesktopSettingsStore()
    val theme = arg("--theme")
    val size = arg("--size")?.split('x')?.mapNotNull { it.toIntOrNull() }?.takeIf { it.size == 2 } ?: listOf(1500, 950)
    val shot = arg("--shot")
    val explainPrompt = arg("--explain-prompt") ?: "What is the capital of France?"
    data class TourStep(val sec: Long, val screen: Screen, val selectFirstDecision: Boolean, val path: String)
    val tour = arg("--tour")?.split(';')?.mapNotNull { entry ->
        val (secScreen, path) = entry.split(':', limit = 2).takeIf { it.size == 2 } ?: return@mapNotNull null
        val (secText, screenName) = secScreen.split('=', limit = 2).takeIf { it.size == 2 } ?: return@mapNotNull null
        val sec = secText.trim().toLongOrNull() ?: return@mapNotNull null
        val name = screenName.trim().lowercase()
        val screen = when (name) {
            "decisions", "decision" -> Screen.Decisions
            "roster" -> Screen.Roster
            "explain" -> Screen.Explain
            "setup" -> Screen.Setup
            else -> return@mapNotNull null
        }
        TourStep(sec, screen, selectFirstDecision = name == "decision", path.trim())
    }?.sortedBy { it.sec }.orEmpty()

    application {
        val state = rememberWindowState(size = DpSize(size[0].dp, size[1].dp))
        Window(onCloseRequest = ::exitApplication, title = "ModelRouter", state = state) {
            val dark = when (theme) { "dark" -> true; "light" -> false; else -> isSystemInDarkTheme() }
            val controllerRef = androidx.compose.runtime.remember { arrayOfNulls<AppController>(1) }
            ModelRouterApp(store, dark = dark, onController = { controllerRef[0] = it })
            if (tour.isNotEmpty()) {
                val w = window
                androidx.compose.runtime.LaunchedEffect(Unit) {
                    val t0 = System.currentTimeMillis()
                    w.isAlwaysOnTop = true
                    for (step in tour) {
                        val wait = step.sec * 1000 - (System.currentTimeMillis() - t0)
                        if (wait > 0) kotlinx.coroutines.delay(wait)
                        val ctl = controllerRef[0]
                        ctl?.screen = step.screen
                        if (step.screen == Screen.Explain && ctl != null && ctl.explainResult == null && !ctl.explainLoading) {
                            ctl.explainPrompt = explainPrompt
                            ctl.explainMaxTokens = "512" // show the budget actually sent, so the capture is honest
                            ctl.runExplain(explainPrompt, 512, false)
                        }
                        kotlinx.coroutines.delay(2500) // let data load and recompose settle
                        if (step.selectFirstDecision && ctl != null) {
                            val id = ctl.decisions.firstOrNull()?.id
                            if (id != null) { ctl.openDecision(id); kotlinx.coroutines.delay(700) }
                        }
                        w.toFront()
                        val p = w.locationOnScreen
                        val img = Robot().createScreenCapture(Rectangle(p.x, p.y, w.width, w.height))
                        File(step.path).absoluteFile.parentFile?.mkdirs()
                        ImageIO.write(img, "png", File(step.path))
                        println("screenshot at ${step.sec}s (${step.screen}) written: ${File(step.path).absolutePath}")
                    }
                    exitApplication()
                }
            }
            if (shot != null) {
                val w = window
                androidx.compose.runtime.LaunchedEffect(Unit) {
                    kotlinx.coroutines.delay(3500)
                    w.isAlwaysOnTop = true
                    w.toFront()
                    kotlinx.coroutines.delay(500)
                    val p = w.locationOnScreen
                    val img = Robot().createScreenCapture(Rectangle(p.x, p.y, w.width, w.height))
                    File(shot).absoluteFile.parentFile?.mkdirs()
                    ImageIO.write(img, "png", File(shot))
                    println("screenshot written: ${File(shot).absolutePath} (${w.width}x${w.height})")
                    exitApplication()
                }
            }
        }
    }
}
