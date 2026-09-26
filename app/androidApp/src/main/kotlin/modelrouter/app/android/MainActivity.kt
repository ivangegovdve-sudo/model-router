package modelrouter.app.android

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.safeDrawingPadding
import androidx.compose.ui.Modifier
import modelrouter.app.AndroidSettingsStore
import modelrouter.app.ui.ModelRouterApp

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        val store = AndroidSettingsStore(applicationContext)
        setContent {
            Box(Modifier.fillMaxSize().safeDrawingPadding()) {
                ModelRouterApp(store, dark = isSystemInDarkTheme())
            }
        }
    }
}
