package modelrouter.app

import android.content.Context
import android.content.SharedPreferences
import androidx.security.crypto.EncryptedSharedPreferences
import androidx.security.crypto.MasterKey
import io.ktor.client.engine.HttpClientEngineFactory
import io.ktor.client.engine.cio.CIO

actual fun httpEngine(): HttpClientEngineFactory<*> = CIO

/** Android settings: everything in EncryptedSharedPreferences (AES256-GCM values, keys in the Android Keystore). */
@Suppress("DEPRECATION")
class AndroidSettingsStore(context: Context) : SettingsStore {
    private val prefs: SharedPreferences = EncryptedSharedPreferences.create(
        context,
        "modelrouter_secure",
        MasterKey.Builder(context).setKeyScheme(MasterKey.KeyScheme.AES256_GCM).build(),
        EncryptedSharedPreferences.PrefKeyEncryptionScheme.AES256_SIV,
        EncryptedSharedPreferences.PrefValueEncryptionScheme.AES256_GCM,
    )

    override val storageNote: String = "Stored in EncryptedSharedPreferences (Android Keystore)"

    override fun load(): RouterSettings = RouterSettings(
        baseUrl = prefs.getString("baseUrl", null) ?: RouterSettings.DEFAULT_URL,
        token = prefs.getString("token", null) ?: "",
    )

    override fun save(settings: RouterSettings) {
        prefs.edit()
            .putString("baseUrl", settings.baseUrl)
            .putString("token", settings.token)
            .apply()
    }
}
