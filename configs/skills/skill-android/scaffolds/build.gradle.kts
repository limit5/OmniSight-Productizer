// SKILL-ANDROID (P8 #293) — root build.gradle.kts.
//
// Empty root block: all plugin versions are declared here with `apply
// false` so each module applies them without re-resolving. This is the
// pattern AGP 7.4+ recommends for multi-module projects.

plugins {
    id("com.android.application") version "8.3.2" apply false
    id("com.android.library") version "8.3.2" apply false
    id("org.jetbrains.kotlin.android") version "2.0.0" apply false
    id("org.jetbrains.kotlin.plugin.compose") version "2.0.0" apply false
}

// Clean task for convenience; matches the legacy groovy-DSL template.
tasks.register("clean", Delete::class) {
    delete(rootProject.layout.buildDirectory)
}
