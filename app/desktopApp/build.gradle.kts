import org.jetbrains.compose.desktop.application.dsl.TargetFormat

plugins {
    alias(libs.plugins.kotlinJvm)
    alias(libs.plugins.composeCompiler)
    alias(libs.plugins.composeMultiplatform)
}

kotlin { jvmToolchain(17) }

dependencies {
    implementation(project(":shared"))
    implementation(compose.desktop.currentOs)
    implementation(libs.coroutines.swing)
}

compose.desktop {
    application {
        mainClass = "modelrouter.app.desktop.MainKt"
        jvmArgs += listOf("-Xmx512m")
        nativeDistributions {
            targetFormats(TargetFormat.Msi)
            packageName = "ModelRouter"
            packageVersion = "1.0.0"
            description = "modelrouter decision viewer"
            vendor = "Ivan Gegov"
            modules("java.naming", "jdk.crypto.ec", "jdk.unsupported")
            windows {
                menuGroup = "ModelRouter"
                upgradeUuid = "9d3a5f2e-1b7c-4a6d-8e42-6c1f8a2b3d90"
                perUserInstall = true
                shortcut = true
            }
        }
    }
}
