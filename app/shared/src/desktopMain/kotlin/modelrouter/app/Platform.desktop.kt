package modelrouter.app

import com.sun.jna.platform.win32.Crypt32Util
import io.ktor.client.engine.HttpClientEngineFactory
import io.ktor.client.engine.cio.CIO
import java.io.File
import java.util.Properties

actual fun httpEngine(): HttpClientEngineFactory<*> = CIO

/**
 * Desktop settings: URL in a properties file, token encrypted with Windows DPAPI
 * (CryptProtectData, current-user scope) in a separate binary file under %APPDATA%\ModelRouter.
 * On non-Windows hosts the token is kept in memory only and never written.
 */
class DesktopSettingsStore(
    private val dir: File = defaultDir(),
) : SettingsStore {
    private val propsFile = File(dir, "settings.properties")
    private val secretFile = File(dir, "token.dpapi")
    private val isWindows = System.getProperty("os.name").orEmpty().startsWith("Windows", ignoreCase = true)
    private var memoryToken: String = ""

    override val storageNote: String
        get() = if (isWindows) "Token encrypted with Windows DPAPI (current user) in ${secretFile.path}"
        else "Token kept in memory only on this OS"

    override fun load(): RouterSettings {
        val p = Properties()
        if (propsFile.exists()) propsFile.inputStream().use { p.load(it) }
        val token = if (isWindows && secretFile.exists()) {
            runCatching { String(Crypt32Util.cryptUnprotectData(secretFile.readBytes()), Charsets.UTF_8) }.getOrDefault("")
        } else memoryToken
        return RouterSettings(
            baseUrl = p.getProperty("baseUrl") ?: RouterSettings.DEFAULT_URL,
            token = token,
        )
    }

    override fun save(settings: RouterSettings) {
        dir.mkdirs()
        val p = Properties()
        p.setProperty("baseUrl", settings.baseUrl)
        propsFile.outputStream().use { p.store(it, "ModelRouter (token is not in this file)") }
        if (isWindows) {
            if (settings.token.isEmpty()) secretFile.delete()
            else secretFile.writeBytes(Crypt32Util.cryptProtectData(settings.token.toByteArray(Charsets.UTF_8)))
        } else memoryToken = settings.token
    }

    companion object {
        fun defaultDir(): File {
            val appData = System.getenv("APPDATA")
            return if (appData != null) File(appData, "ModelRouter") else File(System.getProperty("user.home"), ".modelrouter")
        }
    }
}
