"""V5 #2 (issue #321) — three-platform mobile component registry.

Canonical inventory of the mobile UI primitives — **SwiftUI 6 views**
(iOS 16+), **Jetpack Compose Material 3** (compileSdk 35 / minSdk 24),
and **Flutter widgets** (3.22+) — that the V5 **Mobile UI Designer**
agent (see ``configs/roles/mobile-ui-designer.md``) MUST call
``get_mobile_components()`` against before emitting any Swift / Kotlin
/ Dart code.  The registry is the agent's source of ground truth, so
it never falls back to training-memory APIs (``NavigationView`` is
deprecated, ``BottomNavigationBar`` is M2-legacy, ``Tab`` is iOS 18+,
etc.).

Why a Python module instead of expanding the role file inline
-------------------------------------------------------------

The role file (``configs/roles/mobile-ui-designer.md``) is immutable
LLM context — it must stay compact (~400 lines) and may not bloat
across the three platforms it covers.  This registry is structured
data (dataclasses → dicts → JSON) that can be sliced by platform or
category and injected into the agent's context on demand.

* The Edit complexity auto-router calls
  :func:`render_agent_context_block` with ``platforms=("swiftui",)``
  for a Haiku micro-edit (smaller token budget) and the full three
  platforms for an Opus full-page emit.
* The sibling V5 workers (Figma → mobile, Screenshot → mobile, the
  per-platform engineer skills) feed off the same data so naming
  stays canonical.
* A new platform component (e.g. M3 ``SegmentedButton`` shipped in
  1.2) becomes available to every consumer by adding one entry here.

Contract (pinned by ``backend/tests/test_mobile_component_registry.py``)
-----------------------------------------------------------------------

* Every entry has a non-empty platform / category / summary / signature
  / example, plus an explicit min-platform-version anchor.
* Each platform must populate every category at least once (so the
  agent can offer cross-platform parity for any layout intent).
* :func:`get_mobile_components` returns JSON-serialisable dicts only
  (no dataclass instances leak across the tool boundary).
* :func:`render_agent_context_block` is deterministic — two identical
  calls produce byte-identical output (required for Anthropic
  prompt-cache stability).
* No entry hard-codes hex / pt / dp / sp values in its example —
  examples cite ``MaterialTheme.colorScheme.*`` / ``Theme.of(context)``
  / SF Symbols / token references only.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from typing import Iterable

logger = logging.getLogger(__name__)

# Bump when the schema of a registry entry changes.  Consumers cache
# render output keyed on this version.
REGISTRY_SCHEMA_VERSION = "1.0.0"


# ── Fixed taxonomies ─────────────────────────────────────────────────


#: Three target platforms.  The mobile-ui-designer role emits all
#: three by default; Edit auto-router may narrow with a ``platforms=``
#: filter when user pinned one target (``"用 SwiftUI"``).
PLATFORMS: tuple[str, ...] = (
    "swiftui",   # iOS 16+ / iPadOS / Mac Catalyst
    "compose",   # Jetpack Compose Material 3 (compileSdk 35 / minSdk 24)
    "flutter",   # Flutter 3.22+ (Material 3 + Cupertino)
)

#: Canonical platform display labels used by render helpers.
PLATFORM_LABELS: dict[str, str] = {
    "swiftui": "SwiftUI (iOS 16+)",
    "compose": "Jetpack Compose Material 3",
    "flutter": "Flutter 3.22+",
}

#: Cross-platform category taxonomy.  These mirror the web shadcn
#: registry's categories so a designer can think in one mental model.
#: ``"layout"`` covers scaffolds + containers + lists (the structural
#: spine of a screen); ``"navigation"`` covers tab bars / nav rails /
#: routes; ``"inputs"`` covers controls; ``"overlay"`` covers modals
#: + popovers + dropdowns; ``"feedback"`` covers progress + snackbars
#: + dialogs that *report* state; ``"data"`` covers display-only
#: primitives.
CATEGORIES: tuple[str, ...] = (
    "layout",
    "navigation",
    "inputs",
    "overlay",
    "feedback",
    "data",
)


# ── Data model ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class MobileComponent:
    """Registry entry for one platform component / view / widget.

    ``name`` is the canonical platform identifier (``"NavigationStack"``
    for SwiftUI, ``"Scaffold"`` for Compose, ``"ListView.builder"`` for
    Flutter).  Compose entries that come in a small family share one
    entry (e.g. ``"TopAppBar"`` covers the Center/Medium/Large
    variants — the variants list enumerates them).

    ``signature`` is a one-line, agent-readable invocation shape (not
    valid platform syntax — just a hint).  ``example`` is the runnable
    snippet the agent should pattern-match.
    """

    name: str
    platform: str
    category: str
    summary: str
    signature: str
    example: str
    min_version: str
    a11y: str = ""
    variants: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    deprecates: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.platform not in PLATFORMS:
            raise ValueError(
                f"{self.name}: platform {self.platform!r} not in {PLATFORMS}"
            )
        if self.category not in CATEGORIES:
            raise ValueError(
                f"{self.name}: category {self.category!r} not in {CATEGORIES}"
            )
        if not self.summary.strip():
            raise ValueError(f"{self.name}: summary must be non-empty")
        if not self.signature.strip():
            raise ValueError(f"{self.name}: signature must be non-empty")
        if not self.example.strip():
            raise ValueError(f"{self.name}: example must be non-empty")
        if not self.min_version.strip():
            raise ValueError(f"{self.name}: min_version must be non-empty")

    @property
    def key(self) -> str:
        """Unique composite key (``"swiftui:NavigationStack"``)."""
        return f"{self.platform}:{self.name}"


def _c(**kw) -> MobileComponent:
    return MobileComponent(**kw)


# ── SwiftUI catalogue (iOS 16+) ──────────────────────────────────────
#
# Sourced from configs/roles/mobile-ui-designer.md §SwiftUI.  Hard rule:
# every entry must reflect iOS 16+ canonical API.  ``NavigationView``
# is intentionally absent — it is the deprecated form and the registry
# must not surface it (the agent would happily resurrect it from
# training memory if we left a hole).

_SWIFTUI: tuple[MobileComponent, ...] = (
    # ── layout (scaffolds + containers + lists) ───────────────────
    _c(
        name="NavigationStack",
        platform="swiftui",
        category="layout",
        summary="Stack-based navigation root (replaces NavigationView).",
        signature="NavigationStack { content }",
        example=(
            "NavigationStack {\n"
            "  List(items) { item in\n"
            "    NavigationLink(item.title, value: item)\n"
            "  }\n"
            "  .navigationDestination(for: Item.self) { ItemDetail(item: $0) }\n"
            "}"
        ),
        min_version="iOS 16",
        deprecates=("NavigationView",),
        notes=(
            "Use NavigationSplitView on iPad / Mac Catalyst for two-/three-column layouts.",
            "Push state with .navigationDestination(for:) rather than NavigationLink(destination:).",
        ),
    ),
    _c(
        name="NavigationSplitView",
        platform="swiftui",
        category="layout",
        summary="Two- or three-column responsive navigation (sidebar + detail).",
        signature="NavigationSplitView { sidebar } content: { content } detail: { detail }",
        example=(
            "NavigationSplitView {\n"
            "  List(folders) { folder in NavigationLink(folder.name, value: folder) }\n"
            "} detail: {\n"
            "  if let folder = selected { FolderView(folder: folder) }\n"
            "}"
        ),
        min_version="iOS 16",
        notes=("Adapts to compact size class by collapsing into a NavigationStack automatically.",),
    ),
    _c(
        name="TabView",
        platform="swiftui",
        category="navigation",
        summary="Tab-bar root container; iOS 18+ uses Tab(...) value-based API.",
        signature="TabView(selection:) { Tab(...) { ... } ... }",
        example=(
            "TabView(selection: $tab) {\n"
            "  Tab(\"Home\", systemImage: \"house\", value: .home) { HomeView() }\n"
            "  Tab(\"Inbox\", systemImage: \"tray\", value: .inbox) { InboxView() }\n"
            "}"
        ),
        min_version="iOS 16 (Tab() API: iOS 18)",
        variants=(".tabViewStyle(.page) for paged carousels",),
        notes=(
            "Pre-iOS-18: use .tabItem { Label(\"Home\", systemImage: \"house\") } on each child.",
        ),
    ),
    _c(
        name="ScrollView",
        platform="swiftui",
        category="layout",
        summary="Scrolling container; pair with LazyV/HStack for >20 children.",
        signature="ScrollView(.vertical | .horizontal) { content }",
        example=(
            "ScrollView {\n"
            "  LazyVStack(spacing: 12) {\n"
            "    ForEach(items) { ItemRow(item: $0) }\n"
            "  }\n"
            "  .padding(.horizontal)\n"
            "}"
        ),
        min_version="iOS 16",
        notes=("Plain VStack inside ScrollView eagerly renders all children — use LazyVStack.",),
    ),
    _c(
        name="LazyVStack",
        platform="swiftui",
        category="layout",
        summary="Lazy vertical stack inside ScrollView; renders rows on demand.",
        signature="LazyVStack(alignment:, spacing:, pinnedViews:) { ForEach(...) { ... } }",
        example=(
            "LazyVStack(spacing: 8, pinnedViews: [.sectionHeaders]) {\n"
            "  ForEach(sections) { section in\n"
            "    Section(header: SectionHeader(section)) { ForEach(section.items) { ItemRow($0) } }\n"
            "  }\n"
            "}"
        ),
        min_version="iOS 16",
    ),
    _c(
        name="LazyVGrid",
        platform="swiftui",
        category="layout",
        summary="Lazy vertical grid with adaptive / fixed / flexible columns.",
        signature="LazyVGrid(columns: [GridItem], spacing:) { ForEach(...) { ... } }",
        example=(
            "let columns = [GridItem(.adaptive(minimum: 120), spacing: 12)]\n"
            "LazyVGrid(columns: columns, spacing: 12) {\n"
            "  ForEach(photos) { Photo($0) }\n"
            "}"
        ),
        min_version="iOS 16",
    ),
    _c(
        name="List",
        platform="swiftui",
        category="layout",
        summary="Semantic table-of-rows view; supports section + swipe + edit.",
        signature="List(data, id:) { row } / List { Section { ... } }",
        example=(
            "List {\n"
            "  Section(\"Inbox\") {\n"
            "    ForEach(messages) { msg in\n"
            "      Label(msg.subject, systemImage: \"envelope\")\n"
            "    }\n"
            "    .onDelete(perform: delete)\n"
            "  }\n"
            "}\n"
            ".listStyle(.insetGrouped)"
        ),
        min_version="iOS 16",
        a11y="Each Label automatically announces icon + text; rows expose .accessibilityHint.",
    ),
    _c(
        name="Form",
        platform="swiftui",
        category="layout",
        summary="Settings / preference grouped layout with built-in Dynamic Type.",
        signature="Form { Section { ... } }",
        example=(
            "Form {\n"
            "  Section(\"Account\") {\n"
            "    TextField(\"Name\", text: $name)\n"
            "    Toggle(\"Notifications\", isOn: $notify)\n"
            "  }\n"
            "  Section { Button(\"Sign Out\", role: .destructive) { signOut() } }\n"
            "}"
        ),
        min_version="iOS 16",
    ),
    _c(
        name="Section",
        platform="swiftui",
        category="layout",
        summary="Logical grouping inside List / Form / Picker / LazyV*.",
        signature="Section(header:, footer:) { content } / Section(\"Title\") { ... }",
        example='Section("General") { Toggle("Dark mode", isOn: $isDark) }',
        min_version="iOS 16",
    ),
    _c(
        name="VStack",
        platform="swiftui",
        category="layout",
        summary="Vertical layout primitive (eager — use LazyVStack inside ScrollView).",
        signature="VStack(alignment:, spacing:) { ... }",
        example='VStack(alignment: .leading, spacing: 8) { Text("Title").font(.headline); Text("Body") }',
        min_version="iOS 16",
    ),
    _c(
        name="HStack",
        platform="swiftui",
        category="layout",
        summary="Horizontal layout primitive.",
        signature="HStack(alignment:, spacing:) { ... }",
        example='HStack(spacing: 12) { Image(systemName: "person"); Text("Profile") }',
        min_version="iOS 16",
    ),
    _c(
        name="ZStack",
        platform="swiftui",
        category="layout",
        summary="Depth (Z-axis) overlay primitive.",
        signature="ZStack(alignment:) { ... }",
        example='ZStack(alignment: .topTrailing) { CardView(); Badge("New") }',
        min_version="iOS 16",
    ),
    _c(
        name="Grid",
        platform="swiftui",
        category="layout",
        summary="Table-style grid with column-aligned cells (iOS 16+).",
        signature="Grid(alignment:, horizontalSpacing:, verticalSpacing:) { GridRow { ... } }",
        example=(
            "Grid {\n"
            "  GridRow { Text(\"Mon\"); Text(\"42\") }\n"
            "  GridRow { Text(\"Tue\"); Text(\"58\") }\n"
            "}"
        ),
        min_version="iOS 16",
    ),
    # ── inputs ────────────────────────────────────────────────────
    _c(
        name="Button",
        platform="swiftui",
        category="inputs",
        summary="Tap target; pair with .buttonStyle for visual variant.",
        signature="Button(role:, action:) { Label }",
        example=(
            "Button(role: .destructive, action: delete) {\n"
            "  Label(\"Delete\", systemImage: \"trash\")\n"
            "}\n"
            ".buttonStyle(.borderedProminent)"
        ),
        min_version="iOS 16",
        variants=(
            ".buttonStyle(.borderedProminent)",
            ".buttonStyle(.bordered)",
            ".buttonStyle(.plain)",
        ),
        a11y="Icon-only buttons MUST set .accessibilityLabel; .buttonStyle preserves the 44pt hit target.",
    ),
    _c(
        name="TextField",
        platform="swiftui",
        category="inputs",
        summary="Single-line text input; pair with TextFieldStyle + keyboard hints.",
        signature="TextField(_:, text:, prompt:)",
        example=(
            "TextField(\"Email\", text: $email)\n"
            "  .textFieldStyle(.roundedBorder)\n"
            "  .textInputAutocapitalization(.never)\n"
            "  .keyboardType(.emailAddress)\n"
            "  .autocorrectionDisabled()"
        ),
        min_version="iOS 16",
    ),
    _c(
        name="SecureField",
        platform="swiftui",
        category="inputs",
        summary="Masked text input for passwords / PINs.",
        signature="SecureField(_:, text:)",
        example='SecureField("Password", text: $password).textFieldStyle(.roundedBorder)',
        min_version="iOS 16",
    ),
    _c(
        name="Toggle",
        platform="swiftui",
        category="inputs",
        summary="Binary switch; idiomatic for settings rows.",
        signature="Toggle(_:, isOn:)",
        example='Toggle("Push notifications", isOn: $notify).tint(.accentColor)',
        min_version="iOS 16",
        a11y="Auto-announces 'on'/'off'; pair with Label for icon + text.",
    ),
    _c(
        name="Picker",
        platform="swiftui",
        category="inputs",
        summary="Single-select from enum / collection; .menu / .segmented / .wheel / .navigationLink.",
        signature="Picker(_:, selection:) { ForEach { Text(...).tag(value) } }",
        example=(
            "Picker(\"Theme\", selection: $theme) {\n"
            "  ForEach(Theme.allCases, id: \\.self) { Text($0.label).tag($0) }\n"
            "}\n"
            ".pickerStyle(.segmented)"
        ),
        min_version="iOS 16",
    ),
    _c(
        name="Slider",
        platform="swiftui",
        category="inputs",
        summary="Continuous numeric range input.",
        signature="Slider(value:, in:, step:)",
        example='Slider(value: $volume, in: 0...100, step: 1).accessibilityValue("\\(Int(volume)) percent")',
        min_version="iOS 16",
    ),
    _c(
        name="Stepper",
        platform="swiftui",
        category="inputs",
        summary="Discrete +/- value adjuster.",
        signature="Stepper(value:, in:, step:)",
        example='Stepper("Quantity: \\(qty)", value: $qty, in: 1...99)',
        min_version="iOS 16",
    ),
    _c(
        name="DatePicker",
        platform="swiftui",
        category="inputs",
        summary="Date / time picker; .compact / .graphical / .wheel styles.",
        signature="DatePicker(_:, selection:, displayedComponents:)",
        example=(
            "DatePicker(\"Due\", selection: $due, displayedComponents: [.date, .hourAndMinute])\n"
            "  .datePickerStyle(.compact)"
        ),
        min_version="iOS 16",
    ),
    _c(
        name="Menu",
        platform="swiftui",
        category="overlay",
        summary="Tap-triggered action menu (replaces UIMenu actions).",
        signature="Menu(_:) { Button(...) }",
        example=(
            "Menu(\"Sort\") {\n"
            "  Button(\"By date\") { sort = .date }\n"
            "  Button(\"By size\") { sort = .size }\n"
            "}"
        ),
        min_version="iOS 16",
    ),
    _c(
        name="Link",
        platform="swiftui",
        category="navigation",
        summary="External URL link; opens via system handler.",
        signature="Link(_:, destination:)",
        example='Link("Open docs", destination: URL(string: "https://example.com")!)',
        min_version="iOS 16",
        a11y="Auto-marks as link trait; ensure label describes destination, not 'click here'.",
    ),
    _c(
        name="ShareLink",
        platform="swiftui",
        category="navigation",
        summary="System share sheet trigger.",
        signature="ShareLink(item:, subject:, message:)",
        example='ShareLink(item: url, subject: Text("Read this"))',
        min_version="iOS 16",
    ),
    # ── overlays ──────────────────────────────────────────────────
    _c(
        name="alert",
        platform="swiftui",
        category="overlay",
        summary="System alert dialog; required for destructive confirms.",
        signature=".alert(_:, isPresented:, actions:, message:)",
        example=(
            ".alert(\"Delete file?\", isPresented: $confirming) {\n"
            "  Button(\"Delete\", role: .destructive) { delete() }\n"
            "  Button(\"Cancel\", role: .cancel) {}\n"
            "} message: { Text(\"This cannot be undone.\") }"
        ),
        min_version="iOS 16",
        a11y="Trap focus + announce via VoiceOver automatically; never recreate with ZStack.",
    ),
    _c(
        name="confirmationDialog",
        platform="swiftui",
        category="overlay",
        summary="Action sheet for choosing between options (iOS bottom-sheet style).",
        signature=".confirmationDialog(_:, isPresented:, titleVisibility:, actions:)",
        example=(
            ".confirmationDialog(\"Move to…\", isPresented: $picking) {\n"
            "  Button(\"Inbox\") { move(.inbox) }\n"
            "  Button(\"Archive\") { move(.archive) }\n"
            "  Button(\"Cancel\", role: .cancel) {}\n"
            "}"
        ),
        min_version="iOS 16",
    ),
    _c(
        name="sheet",
        platform="swiftui",
        category="overlay",
        summary="Bottom-sheet modal presentation.",
        signature=".sheet(isPresented: | item:) { content }",
        example=(
            ".sheet(isPresented: $showing) {\n"
            "  EditView().presentationDetents([.medium, .large])\n"
            "}"
        ),
        min_version="iOS 16",
        notes=(".presentationDetents([.medium, .large]) gives the iOS 16 resizable behavior.",),
    ),
    _c(
        name="popover",
        platform="swiftui",
        category="overlay",
        summary="Anchored popover; iPad uses arrow, iPhone falls back to sheet.",
        signature=".popover(isPresented:, attachmentAnchor:) { content }",
        example=".popover(isPresented: $showing) { FilterView() }",
        min_version="iOS 16",
    ),
    _c(
        name="toolbar",
        platform="swiftui",
        category="navigation",
        summary="Nav-bar / bottom-bar / keyboard accessory items.",
        signature=".toolbar { ToolbarItem(placement:) { ... } }",
        example=(
            ".toolbar {\n"
            "  ToolbarItem(placement: .topBarTrailing) {\n"
            "    Button(action: save) { Image(systemName: \"square.and.arrow.down\") }\n"
            "      .accessibilityLabel(\"Save\")\n"
            "  }\n"
            "}"
        ),
        min_version="iOS 16",
        a11y="Icon-only toolbar buttons MUST carry .accessibilityLabel.",
    ),
    # ── feedback ──────────────────────────────────────────────────
    _c(
        name="ProgressView",
        platform="swiftui",
        category="feedback",
        summary="Determinate / indeterminate progress; .circular or .linear.",
        signature="ProgressView(value:, total:) / ProgressView()",
        example='ProgressView(value: progress, total: 1.0).progressViewStyle(.linear)',
        min_version="iOS 16",
    ),
    _c(
        name="Label",
        platform="swiftui",
        category="data",
        summary="Icon + text canonical pair (replaces ad-hoc HStack { Image; Text }).",
        signature="Label(_:, systemImage:)",
        example='Label("Inbox", systemImage: "tray")',
        min_version="iOS 16",
        a11y="Combines icon and text into a single accessibility element automatically.",
    ),
    _c(
        name="Image(systemName:)",
        platform="swiftui",
        category="data",
        summary="SF Symbols glyph; auto-scales with Dynamic Type.",
        signature='Image(systemName: "...")',
        example='Image(systemName: "person.crop.circle").symbolRenderingMode(.hierarchical)',
        min_version="iOS 16",
        a11y="Decorative SF Symbols MUST set .accessibilityHidden(true); meaningful ones get .accessibilityLabel.",
    ),
    _c(
        name="@Observable",
        platform="swiftui",
        category="data",
        summary="iOS 17+ observation macro replacing ObservableObject + @Published.",
        signature="@Observable class ViewModel { var x: Int = 0 }",
        example=(
            "@Observable class CounterModel { var count = 0 }\n"
            "// in view: @State private var model = CounterModel()\n"
            "Text(\"\\(model.count)\")"
        ),
        min_version="iOS 17",
        deprecates=("ObservableObject + @Published",),
        notes=("Use @State to own the instance, @Bindable to derive bindings.",),
    ),
)


# ── Jetpack Compose Material 3 catalogue (compileSdk 35 / minSdk 24) ──

_COMPOSE: tuple[MobileComponent, ...] = (
    # ── layout ────────────────────────────────────────────────────
    _c(
        name="Scaffold",
        platform="compose",
        category="layout",
        summary="Material 3 page wrapper (topBar / bottomBar / FAB / snackbarHost).",
        signature="Scaffold(topBar=, bottomBar=, snackbarHost=, floatingActionButton=, content=)",
        example=(
            "Scaffold(\n"
            "  topBar = { TopAppBar(title = { Text(\"Home\") }) },\n"
            "  bottomBar = { NavigationBar { /* items */ } },\n"
            "  snackbarHost = { SnackbarHost(snackbarHostState) },\n"
            "  floatingActionButton = { FloatingActionButton(onClick = ::add) { Icon(Icons.Default.Add, contentDescription = \"Add\") } },\n"
            ") { padding ->\n"
            "  LazyColumn(modifier = Modifier.padding(padding)) { /* rows */ }\n"
            "}"
        ),
        min_version="compose-material3 1.2",
        notes=(
            "Always thread the inner padding to the content lambda — never ignore it.",
            "Edge-to-edge: pair with enableEdgeToEdge() in Activity.onCreate (Android 15 mandatory).",
        ),
    ),
    _c(
        name="Surface",
        platform="compose",
        category="layout",
        summary="Material container (color + shape + tonal elevation).",
        signature="Surface(modifier=, color=, tonalElevation=, shape=) { content }",
        example=(
            "Surface(\n"
            "  color = MaterialTheme.colorScheme.surfaceVariant,\n"
            "  tonalElevation = 3.dp,\n"
            "  shape = MaterialTheme.shapes.medium,\n"
            ") { Text(\"Hello\", modifier = Modifier.padding(16.dp)) }"
        ),
        min_version="compose-material3 1.2",
        notes=("M3 prefers tonalElevation over shadow elevation for dark-theme parity.",),
    ),
    _c(
        name="Box",
        platform="compose",
        category="layout",
        summary="Z-axis layout primitive (overlay / align children).",
        signature="Box(modifier=, contentAlignment=) { children }",
        example=(
            "Box(modifier = Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {\n"
            "  CircularProgressIndicator()\n"
            "}"
        ),
        min_version="compose 1.6",
    ),
    _c(
        name="Column",
        platform="compose",
        category="layout",
        summary="Vertical layout primitive (eager — use LazyColumn inside scrolling).",
        signature="Column(modifier=, verticalArrangement=, horizontalAlignment=) { ... }",
        example=(
            "Column(\n"
            "  modifier = Modifier.fillMaxWidth().padding(16.dp),\n"
            "  verticalArrangement = Arrangement.spacedBy(8.dp),\n"
            ") { Text(\"Title\", style = MaterialTheme.typography.headlineSmall) }"
        ),
        min_version="compose 1.6",
    ),
    _c(
        name="Row",
        platform="compose",
        category="layout",
        summary="Horizontal layout primitive.",
        signature="Row(modifier=, horizontalArrangement=, verticalAlignment=) { ... }",
        example=(
            "Row(verticalAlignment = Alignment.CenterVertically) {\n"
            "  Icon(Icons.Default.Person, contentDescription = null)\n"
            "  Spacer(Modifier.width(8.dp))\n"
            "  Text(\"Profile\")\n"
            "}"
        ),
        min_version="compose 1.6",
    ),
    _c(
        name="LazyColumn",
        platform="compose",
        category="layout",
        summary="Recycling vertical list; mandatory for >20 items.",
        signature="LazyColumn(modifier=, contentPadding=, verticalArrangement=) { items(...) { ... } }",
        example=(
            "LazyColumn(\n"
            "  contentPadding = PaddingValues(horizontal = 16.dp, vertical = 8.dp),\n"
            "  verticalArrangement = Arrangement.spacedBy(8.dp),\n"
            ") {\n"
            "  items(messages, key = { it.id }) { msg -> MessageRow(msg) }\n"
            "}"
        ),
        min_version="compose-foundation 1.6",
        notes=(
            "Always pass key = { ... } for stable item identity (avoids unnecessary recompose).",
        ),
    ),
    _c(
        name="LazyRow",
        platform="compose",
        category="layout",
        summary="Recycling horizontal list (carousels, chip rails).",
        signature="LazyRow(modifier=, contentPadding=, horizontalArrangement=) { items(...) { ... } }",
        example=(
            "LazyRow(horizontalArrangement = Arrangement.spacedBy(12.dp)) {\n"
            "  items(tags) { tag -> AssistChip(onClick = {}, label = { Text(tag) }) }\n"
            "}"
        ),
        min_version="compose-foundation 1.6",
    ),
    _c(
        name="LazyVerticalGrid",
        platform="compose",
        category="layout",
        summary="Recycling 2-D grid; columns adaptive / fixed.",
        signature="LazyVerticalGrid(columns=GridCells.Adaptive(120.dp)) { items(...) { ... } }",
        example=(
            "LazyVerticalGrid(\n"
            "  columns = GridCells.Adaptive(minSize = 120.dp),\n"
            "  contentPadding = PaddingValues(16.dp),\n"
            "  verticalArrangement = Arrangement.spacedBy(8.dp),\n"
            "  horizontalArrangement = Arrangement.spacedBy(8.dp),\n"
            ") { items(photos) { PhotoTile(it) } }"
        ),
        min_version="compose-foundation 1.6",
    ),
    # ── navigation ────────────────────────────────────────────────
    _c(
        name="TopAppBar",
        platform="compose",
        category="navigation",
        summary="Material 3 top app bar; small / center / medium / large variants.",
        signature="TopAppBar(title=, navigationIcon=, actions=, scrollBehavior=)",
        example=(
            "val scroll = TopAppBarDefaults.exitUntilCollapsedScrollBehavior()\n"
            "LargeTopAppBar(\n"
            "  title = { Text(\"Inbox\") },\n"
            "  navigationIcon = { IconButton(onClick = ::back) { Icon(Icons.AutoMirrored.Filled.ArrowBack, contentDescription = \"Back\") } },\n"
            "  scrollBehavior = scroll,\n"
            ")"
        ),
        min_version="compose-material3 1.2",
        variants=(
            "TopAppBar (small)",
            "CenterAlignedTopAppBar",
            "MediumTopAppBar",
            "LargeTopAppBar",
        ),
        notes=("Use AutoMirrored icons (Filled.ArrowBack) for RTL safety.",),
    ),
    _c(
        name="NavigationBar",
        platform="compose",
        category="navigation",
        summary="M3 bottom navigation bar (replaces M2 BottomNavigation).",
        signature="NavigationBar { NavigationBarItem(selected=, onClick=, icon=, label=) }",
        example=(
            "NavigationBar {\n"
            "  destinations.forEach { dest ->\n"
            "    NavigationBarItem(\n"
            "      selected = current == dest,\n"
            "      onClick = { current = dest },\n"
            "      icon = { Icon(dest.icon, contentDescription = null) },\n"
            "      label = { Text(dest.label) },\n"
            "    )\n"
            "  }\n"
            "}"
        ),
        min_version="compose-material3 1.2",
        deprecates=("BottomNavigation (M2)",),
    ),
    _c(
        name="NavigationRail",
        platform="compose",
        category="navigation",
        summary="Tablet / foldable side navigation rail.",
        signature="NavigationRail { NavigationRailItem(selected=, onClick=, icon=, label=) }",
        example=(
            "NavigationRail(header = { FloatingActionButton(onClick = {}) { Icon(Icons.Default.Add, contentDescription = \"New\") } }) {\n"
            "  destinations.forEach { dest -> NavigationRailItem(selected = ..., onClick = ..., icon = ..., label = ...) }\n"
            "}"
        ),
        min_version="compose-material3 1.2",
        notes=("Pick rail vs bottom NavigationBar via WindowSizeClass.widthSizeClass.",),
    ),
    _c(
        name="ModalNavigationDrawer",
        platform="compose",
        category="navigation",
        summary="Slide-in side drawer (compact size class default).",
        signature="ModalNavigationDrawer(drawerContent=, drawerState=) { content }",
        example=(
            "val drawerState = rememberDrawerState(DrawerValue.Closed)\n"
            "ModalNavigationDrawer(drawerContent = { ModalDrawerSheet { /* items */ } }, drawerState = drawerState) {\n"
            "  Scaffold(/* ... */) { /* content */ }\n"
            "}"
        ),
        min_version="compose-material3 1.2",
    ),
    # ── inputs ────────────────────────────────────────────────────
    _c(
        name="Button",
        platform="compose",
        category="inputs",
        summary="Filled M3 button; pair with FilledTonal / Outlined / Text / Elevated variants.",
        signature="Button(onClick=, enabled=, modifier=, content=)",
        example='Button(onClick = ::save) { Text("Save") }',
        min_version="compose-material3 1.2",
        variants=(
            "Button (filled)",
            "FilledTonalButton",
            "OutlinedButton",
            "TextButton",
            "ElevatedButton",
        ),
        a11y="48dp default hit target — never override Modifier.size below this on icon-only buttons.",
    ),
    _c(
        name="IconButton",
        platform="compose",
        category="inputs",
        summary="Icon-only tap target; preserves 48dp hit area.",
        signature="IconButton(onClick=, modifier=, enabled=) { Icon(...) }",
        example='IconButton(onClick = ::back) { Icon(Icons.AutoMirrored.Filled.ArrowBack, contentDescription = "Back") }',
        min_version="compose-material3 1.2",
        a11y="contentDescription is REQUIRED — do not pass null on actionable buttons.",
    ),
    _c(
        name="FloatingActionButton",
        platform="compose",
        category="inputs",
        summary="Primary screen action FAB; Small / Large / Extended variants.",
        signature="FloatingActionButton(onClick=, modifier=, containerColor=) { Icon(...) }",
        example=(
            "FloatingActionButton(onClick = ::compose) {\n"
            "  Icon(Icons.Default.Edit, contentDescription = \"Compose\")\n"
            "}"
        ),
        min_version="compose-material3 1.2",
        variants=("SmallFloatingActionButton", "LargeFloatingActionButton", "ExtendedFloatingActionButton"),
    ),
    _c(
        name="OutlinedTextField",
        platform="compose",
        category="inputs",
        summary="M3 outlined text input; supports label / supportingText / isError.",
        signature="OutlinedTextField(value=, onValueChange=, label=, supportingText=, isError=, ...)",
        example=(
            "OutlinedTextField(\n"
            "  value = email,\n"
            "  onValueChange = { email = it },\n"
            "  label = { Text(\"Email\") },\n"
            "  supportingText = { Text(\"We never share it.\") },\n"
            "  isError = !isValid,\n"
            "  keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Email),\n"
            "  singleLine = true,\n"
            ")"
        ),
        min_version="compose-material3 1.2",
        a11y="Label semantics auto-wired; isError flips contentDescription to announce validation.",
    ),
    _c(
        name="Checkbox",
        platform="compose",
        category="inputs",
        summary="Tri-state boolean (checked / unchecked / indeterminate).",
        signature="Checkbox(checked=, onCheckedChange=, modifier=)",
        example='Checkbox(checked = agreed, onCheckedChange = { agreed = it })',
        min_version="compose-material3 1.2",
        a11y="When grouped with a Text label, place both inside Modifier.toggleable for one tap target.",
    ),
    _c(
        name="Switch",
        platform="compose",
        category="inputs",
        summary="Binary toggle (M3 thumb + track styling).",
        signature="Switch(checked=, onCheckedChange=)",
        example='Switch(checked = darkMode, onCheckedChange = { darkMode = it })',
        min_version="compose-material3 1.2",
    ),
    _c(
        name="Slider",
        platform="compose",
        category="inputs",
        summary="Continuous / stepped numeric slider.",
        signature="Slider(value=, onValueChange=, valueRange=, steps=)",
        example='Slider(value = volume, onValueChange = { volume = it }, valueRange = 0f..100f)',
        min_version="compose-material3 1.2",
    ),
    _c(
        name="SegmentedButton",
        platform="compose",
        category="inputs",
        summary="Multi-choice segmented control (M3 1.2+).",
        signature="SingleChoiceSegmentedButtonRow { SegmentedButton(selected=, onClick=, shape=) { Text } }",
        example=(
            "SingleChoiceSegmentedButtonRow {\n"
            "  options.forEachIndexed { i, opt ->\n"
            "    SegmentedButton(\n"
            "      selected = selectedIndex == i,\n"
            "      onClick = { selectedIndex = i },\n"
            "      shape = SegmentedButtonDefaults.itemShape(index = i, count = options.size),\n"
            "    ) { Text(opt) }\n"
            "  }\n"
            "}"
        ),
        min_version="compose-material3 1.2",
    ),
    _c(
        name="Chip",
        platform="compose",
        category="inputs",
        summary="M3 chips: Assist / Filter / Input / Suggestion.",
        signature="AssistChip(onClick=, label=) / FilterChip(selected=, onClick=, label=)",
        example=(
            "FilterChip(\n"
            "  selected = isSelected,\n"
            "  onClick = { isSelected = !isSelected },\n"
            "  label = { Text(\"Open now\") },\n"
            "  leadingIcon = if (isSelected) {{ Icon(Icons.Default.Check, contentDescription = null) }} else null,\n"
            ")"
        ),
        min_version="compose-material3 1.2",
        variants=("AssistChip", "FilterChip", "InputChip", "SuggestionChip"),
    ),
    # ── overlays ──────────────────────────────────────────────────
    _c(
        name="AlertDialog",
        platform="compose",
        category="overlay",
        summary="Material 3 modal dialog (basic confirm / form / destructive).",
        signature="AlertDialog(onDismissRequest=, confirmButton=, dismissButton=, title=, text=)",
        example=(
            "AlertDialog(\n"
            "  onDismissRequest = ::dismiss,\n"
            "  title = { Text(\"Delete file?\") },\n"
            "  text = { Text(\"This cannot be undone.\") },\n"
            "  confirmButton = { TextButton(onClick = ::delete) { Text(\"Delete\") } },\n"
            "  dismissButton = { TextButton(onClick = ::dismiss) { Text(\"Cancel\") } },\n"
            ")"
        ),
        min_version="compose-material3 1.2",
        a11y="Focus trap + back-press dismiss are auto-wired; do not roll your own with Box overlay.",
    ),
    _c(
        name="ModalBottomSheet",
        platform="compose",
        category="overlay",
        summary="Material 3 swipe-dismiss bottom sheet.",
        signature="ModalBottomSheet(onDismissRequest=, sheetState=) { content }",
        example=(
            "val sheetState = rememberModalBottomSheetState(skipPartiallyExpanded = false)\n"
            "if (showSheet) {\n"
            "  ModalBottomSheet(onDismissRequest = { showSheet = false }, sheetState = sheetState) {\n"
            "    /* sheet content */\n"
            "  }\n"
            "}"
        ),
        min_version="compose-material3 1.2",
    ),
    _c(
        name="DropdownMenu",
        platform="compose",
        category="overlay",
        summary="Anchored menu under a trigger; pair with ExposedDropdownMenuBox for autocomplete.",
        signature="DropdownMenu(expanded=, onDismissRequest=) { DropdownMenuItem(text=, onClick=) }",
        example=(
            "Box {\n"
            "  IconButton(onClick = { expanded = true }) { Icon(Icons.Default.MoreVert, contentDescription = \"More\") }\n"
            "  DropdownMenu(expanded = expanded, onDismissRequest = { expanded = false }) {\n"
            "    DropdownMenuItem(text = { Text(\"Edit\") }, onClick = ::edit)\n"
            "    DropdownMenuItem(text = { Text(\"Delete\") }, onClick = ::delete)\n"
            "  }\n"
            "}"
        ),
        min_version="compose-material3 1.2",
    ),
    # ── feedback ──────────────────────────────────────────────────
    _c(
        name="SnackbarHost",
        platform="compose",
        category="feedback",
        summary="Snackbar render slot inside Scaffold; transient feedback.",
        signature="SnackbarHost(hostState=) — paired with snackbarHostState.showSnackbar(...)",
        example=(
            "val snackbarHostState = remember { SnackbarHostState() }\n"
            "val scope = rememberCoroutineScope()\n"
            "Scaffold(snackbarHost = { SnackbarHost(snackbarHostState) }) { /* ... */ }\n"
            "// trigger:\n"
            "scope.launch { snackbarHostState.showSnackbar(\"Saved\", actionLabel = \"Undo\") }"
        ),
        min_version="compose-material3 1.2",
    ),
    _c(
        name="LinearProgressIndicator",
        platform="compose",
        category="feedback",
        summary="Determinate or indeterminate horizontal progress bar.",
        signature="LinearProgressIndicator(progress=) / LinearProgressIndicator()",
        example='LinearProgressIndicator(progress = { uploaded / total.toFloat() }, modifier = Modifier.fillMaxWidth())',
        min_version="compose-material3 1.2",
    ),
    _c(
        name="CircularProgressIndicator",
        platform="compose",
        category="feedback",
        summary="Indeterminate / determinate circular spinner.",
        signature="CircularProgressIndicator(progress=) / CircularProgressIndicator()",
        example="CircularProgressIndicator()",
        min_version="compose-material3 1.2",
    ),
    _c(
        name="Badge",
        platform="compose",
        category="feedback",
        summary="Small notification dot / count chip; pair with BadgedBox for anchoring.",
        signature="BadgedBox(badge = { Badge { Text(\"\\$count\") } }) { Icon(...) }",
        example=(
            "BadgedBox(badge = { if (unread > 0) Badge { Text(\"\\$unread\") } }) {\n"
            "  Icon(Icons.Default.Notifications, contentDescription = \"Notifications\")\n"
            "}"
        ),
        min_version="compose-material3 1.2",
    ),
    # ── data ──────────────────────────────────────────────────────
    _c(
        name="Card",
        platform="compose",
        category="data",
        summary="Material 3 card (Elevated / Outlined / Filled).",
        signature="Card(modifier=, shape=, colors=, elevation=) { content }",
        example=(
            "ElevatedCard(modifier = Modifier.fillMaxWidth().padding(16.dp)) {\n"
            "  Column(Modifier.padding(16.dp)) { Text(\"Title\", style = MaterialTheme.typography.titleMedium) }\n"
            "}"
        ),
        min_version="compose-material3 1.2",
        variants=("Card (filled)", "ElevatedCard", "OutlinedCard"),
    ),
    _c(
        name="ListItem",
        platform="compose",
        category="data",
        summary="M3 list row with headline / supporting / leading / trailing slots.",
        signature="ListItem(headlineContent=, supportingContent=, leadingContent=, trailingContent=)",
        example=(
            "ListItem(\n"
            "  headlineContent = { Text(message.subject) },\n"
            "  supportingContent = { Text(message.preview, maxLines = 1) },\n"
            "  leadingContent = { Icon(Icons.Default.Person, contentDescription = null) },\n"
            "  trailingContent = { Text(message.timeAgo) },\n"
            ")"
        ),
        min_version="compose-material3 1.2",
    ),
    _c(
        name="Icon",
        platform="compose",
        category="data",
        summary="Vector / painter icon; tint follows MaterialTheme.colorScheme.",
        signature="Icon(imageVector=, contentDescription=, tint=)",
        example='Icon(Icons.Default.Settings, contentDescription = "Settings")',
        min_version="compose-material3 1.2",
        a11y="contentDescription = null marks the icon decorative; never pass null on standalone icons.",
    ),
    _c(
        name="Text",
        platform="compose",
        category="data",
        summary="M3 text; style via MaterialTheme.typography token (never hard-code sp).",
        signature="Text(text=, style=, color=, modifier=)",
        example='Text("Hello", style = MaterialTheme.typography.headlineSmall, color = MaterialTheme.colorScheme.onSurface)',
        min_version="compose-material3 1.2",
        notes=("Hard-coding sp breaks Font Scale 200% — always use the M3 type scale token.",),
    ),
)


# ── Flutter widgets catalogue (3.22+) ────────────────────────────────

_FLUTTER: tuple[MobileComponent, ...] = (
    # ── layout ────────────────────────────────────────────────────
    _c(
        name="MaterialApp",
        platform="flutter",
        category="layout",
        summary="Material design app root (theme + routing + locale).",
        signature="MaterialApp(theme:, darkTheme:, themeMode:, home: | routes: | routerConfig:)",
        example=(
            "MaterialApp(\n"
            "  theme: ThemeData(useMaterial3: true, colorScheme: lightScheme),\n"
            "  darkTheme: ThemeData(useMaterial3: true, colorScheme: darkScheme),\n"
            "  themeMode: ThemeMode.system,\n"
            "  routerConfig: appRouter,\n"
            ")"
        ),
        min_version="Flutter 3.22",
    ),
    _c(
        name="CupertinoApp",
        platform="flutter",
        category="layout",
        summary="iOS-flavoured app root (Cupertino theme + system font).",
        signature="CupertinoApp(theme:, home:)",
        example=(
            "CupertinoApp(\n"
            "  theme: const CupertinoThemeData(brightness: Brightness.light),\n"
            "  home: HomePage(),\n"
            ")"
        ),
        min_version="Flutter 3.22",
        notes=("Pick MaterialApp OR CupertinoApp per app — never mix navigators in the same tree.",),
    ),
    _c(
        name="Scaffold",
        platform="flutter",
        category="layout",
        summary="Material page wrapper (appBar / body / bottomNavigationBar / FAB / drawer).",
        signature="Scaffold(appBar:, body:, bottomNavigationBar:, floatingActionButton:, drawer:)",
        example=(
            "Scaffold(\n"
            "  appBar: AppBar(title: const Text('Home')),\n"
            "  body: const _HomeBody(),\n"
            "  floatingActionButton: FloatingActionButton(\n"
            "    onPressed: _compose,\n"
            "    tooltip: 'Compose',\n"
            "    child: const Icon(Icons.edit),\n"
            "  ),\n"
            ")"
        ),
        min_version="Flutter 3.22",
    ),
    _c(
        name="CupertinoPageScaffold",
        platform="flutter",
        category="layout",
        summary="iOS-styled page wrapper (CupertinoNavigationBar + body).",
        signature="CupertinoPageScaffold(navigationBar:, child:)",
        example=(
            "CupertinoPageScaffold(\n"
            "  navigationBar: const CupertinoNavigationBar(middle: Text('Settings')),\n"
            "  child: const _Body(),\n"
            ")"
        ),
        min_version="Flutter 3.22",
    ),
    _c(
        name="SafeArea",
        platform="flutter",
        category="layout",
        summary="Pad children to avoid notch / dynamic island / status bar / home indicator.",
        signature="SafeArea(top:, bottom:, left:, right:, child:)",
        example="SafeArea(child: ListView(children: rows))",
        min_version="Flutter 3.22",
        notes=(
            "For finer control use MediaQuery.viewPaddingOf(context) directly on edge-to-edge layouts.",
        ),
    ),
    _c(
        name="Column",
        platform="flutter",
        category="layout",
        summary="Vertical layout primitive (no scrolling — wrap in ListView for long lists).",
        signature="Column(mainAxisAlignment:, crossAxisAlignment:, children:)",
        example=(
            "Column(\n"
            "  crossAxisAlignment: CrossAxisAlignment.start,\n"
            "  children: [\n"
            "    Text('Title', style: theme.textTheme.headlineSmall),\n"
            "    const SizedBox(height: 8),\n"
            "    Text('Body', style: theme.textTheme.bodyMedium),\n"
            "  ],\n"
            ")"
        ),
        min_version="Flutter 3.22",
    ),
    _c(
        name="Row",
        platform="flutter",
        category="layout",
        summary="Horizontal layout primitive.",
        signature="Row(mainAxisAlignment:, crossAxisAlignment:, children:)",
        example=(
            "Row(\n"
            "  children: [\n"
            "    const Icon(Icons.person),\n"
            "    const SizedBox(width: 8),\n"
            "    Text('Profile', style: theme.textTheme.bodyLarge),\n"
            "  ],\n"
            ")"
        ),
        min_version="Flutter 3.22",
    ),
    _c(
        name="Padding",
        platform="flutter",
        category="layout",
        summary="Insets-only wrapper (preferred over Container when only padding is needed).",
        signature="Padding(padding: EdgeInsets, child:)",
        example=(
            "Padding(\n"
            "  padding: EdgeInsets.symmetric(horizontal: AppSpacing.md, vertical: AppSpacing.sm),\n"
            "  child: child,\n"
            ")"
        ),
        min_version="Flutter 3.22",
        notes=("Use AppSpacing tokens — EdgeInsets.all(13.5) is a smell.",),
    ),
    _c(
        name="ListView.builder",
        platform="flutter",
        category="layout",
        summary="Lazily built scrolling list; mandatory for >10 items.",
        signature="ListView.builder(itemCount:, itemBuilder:, padding:, controller:)",
        example=(
            "ListView.builder(\n"
            "  itemCount: messages.length,\n"
            "  padding: const EdgeInsets.symmetric(vertical: 8),\n"
            "  itemBuilder: (context, i) => MessageRow(message: messages[i]),\n"
            ")"
        ),
        min_version="Flutter 3.22",
    ),
    _c(
        name="ListView.separated",
        platform="flutter",
        category="layout",
        summary="ListView.builder + separator widget between rows.",
        signature="ListView.separated(itemCount:, itemBuilder:, separatorBuilder:)",
        example=(
            "ListView.separated(\n"
            "  itemCount: items.length,\n"
            "  itemBuilder: (context, i) => ItemTile(item: items[i]),\n"
            "  separatorBuilder: (_, __) => const Divider(height: 1),\n"
            ")"
        ),
        min_version="Flutter 3.22",
    ),
    _c(
        name="GridView.builder",
        platform="flutter",
        category="layout",
        summary="Lazy grid; SliverGridDelegate decides column layout.",
        signature="GridView.builder(gridDelegate:, itemCount:, itemBuilder:)",
        example=(
            "GridView.builder(\n"
            "  gridDelegate: const SliverGridDelegateWithMaxCrossAxisExtent(\n"
            "    maxCrossAxisExtent: 160,\n"
            "    mainAxisSpacing: 8,\n"
            "    crossAxisSpacing: 8,\n"
            "  ),\n"
            "  itemCount: photos.length,\n"
            "  itemBuilder: (context, i) => PhotoTile(photo: photos[i]),\n"
            ")"
        ),
        min_version="Flutter 3.22",
    ),
    _c(
        name="CustomScrollView",
        platform="flutter",
        category="layout",
        summary="Multi-sliver scrolling (pinned headers, parallax, mixed lists/grids).",
        signature="CustomScrollView(slivers: [SliverAppBar, SliverList, SliverGrid, ...])",
        example=(
            "CustomScrollView(\n"
            "  slivers: [\n"
            "    const SliverAppBar.large(title: Text('Photos'), pinned: true),\n"
            "    SliverList.builder(itemCount: photos.length, itemBuilder: (c, i) => PhotoTile(photo: photos[i])),\n"
            "  ],\n"
            ")"
        ),
        min_version="Flutter 3.22",
    ),
    # ── navigation ────────────────────────────────────────────────
    _c(
        name="AppBar",
        platform="flutter",
        category="navigation",
        summary="Material top app bar.",
        signature="AppBar(title:, leading:, actions:, bottom:)",
        example=(
            "AppBar(\n"
            "  title: const Text('Inbox'),\n"
            "  actions: [\n"
            "    IconButton(\n"
            "      icon: const Icon(Icons.search),\n"
            "      tooltip: 'Search',\n"
            "      onPressed: _search,\n"
            "    ),\n"
            "  ],\n"
            ")"
        ),
        min_version="Flutter 3.22",
        a11y="IconButton MUST set tooltip — TalkBack reads it as the accessibility label.",
    ),
    _c(
        name="SliverAppBar",
        platform="flutter",
        category="navigation",
        summary="Collapsing app bar inside CustomScrollView.",
        signature="SliverAppBar(title:, pinned:, floating:, expandedHeight:, flexibleSpace:)",
        example=(
            "SliverAppBar.large(\n"
            "  title: const Text('Profile'),\n"
            "  pinned: true,\n"
            "  flexibleSpace: const FlexibleSpaceBar(background: _ProfileHeader()),\n"
            ")"
        ),
        min_version="Flutter 3.22",
    ),
    _c(
        name="NavigationBar",
        platform="flutter",
        category="navigation",
        summary="Material 3 bottom navigation (replaces BottomNavigationBar in new code).",
        signature="NavigationBar(selectedIndex:, onDestinationSelected:, destinations:)",
        example=(
            "NavigationBar(\n"
            "  selectedIndex: index,\n"
            "  onDestinationSelected: (i) => setState(() => index = i),\n"
            "  destinations: const [\n"
            "    NavigationDestination(icon: Icon(Icons.home_outlined), selectedIcon: Icon(Icons.home), label: 'Home'),\n"
            "    NavigationDestination(icon: Icon(Icons.inbox_outlined), selectedIcon: Icon(Icons.inbox), label: 'Inbox'),\n"
            "  ],\n"
            ")"
        ),
        min_version="Flutter 3.22",
        deprecates=("BottomNavigationBar (M2 — keep only for legacy apps)",),
    ),
    _c(
        name="NavigationRail",
        platform="flutter",
        category="navigation",
        summary="Tablet / wide-screen side nav rail.",
        signature="NavigationRail(selectedIndex:, onDestinationSelected:, destinations:)",
        example=(
            "NavigationRail(\n"
            "  selectedIndex: index,\n"
            "  onDestinationSelected: (i) => setState(() => index = i),\n"
            "  labelType: NavigationRailLabelType.all,\n"
            "  destinations: const [\n"
            "    NavigationRailDestination(icon: Icon(Icons.home), label: Text('Home')),\n"
            "    NavigationRailDestination(icon: Icon(Icons.inbox), label: Text('Inbox')),\n"
            "  ],\n"
            ")"
        ),
        min_version="Flutter 3.22",
    ),
    _c(
        name="Drawer",
        platform="flutter",
        category="navigation",
        summary="Side drawer attached to Scaffold(drawer:).",
        signature="Drawer(child:)",
        example=(
            "Drawer(\n"
            "  child: ListView(\n"
            "    children: [\n"
            "      const DrawerHeader(child: Text('Account')),\n"
            "      ListTile(leading: const Icon(Icons.settings), title: const Text('Settings'), onTap: () {}),\n"
            "    ],\n"
            "  ),\n"
            ")"
        ),
        min_version="Flutter 3.22",
    ),
    _c(
        name="TabBar",
        platform="flutter",
        category="navigation",
        summary="Top tab bar (pair with TabBarView + DefaultTabController).",
        signature="TabBar(tabs:, controller:) / TabBarView(children:)",
        example=(
            "DefaultTabController(\n"
            "  length: 2,\n"
            "  child: Scaffold(\n"
            "    appBar: AppBar(title: const Text('Lists'), bottom: const TabBar(tabs: [Tab(text: 'Open'), Tab(text: 'Closed')])),\n"
            "    body: const TabBarView(children: [_OpenList(), _ClosedList()]),\n"
            "  ),\n"
            ")"
        ),
        min_version="Flutter 3.22",
    ),
    # ── inputs ────────────────────────────────────────────────────
    _c(
        name="ElevatedButton",
        platform="flutter",
        category="inputs",
        summary="Primary filled button (pre-M3 raised); use FilledButton for M3 emphasis.",
        signature="ElevatedButton(onPressed:, child:, style:)",
        example='ElevatedButton(onPressed: _save, child: const Text("Save"))',
        min_version="Flutter 3.22",
        variants=("FilledButton", "OutlinedButton", "TextButton", "ElevatedButton"),
    ),
    _c(
        name="IconButton",
        platform="flutter",
        category="inputs",
        summary="Icon-only tap target (48dp default hit area).",
        signature="IconButton(onPressed:, icon:, tooltip:)",
        example=(
            "IconButton(\n"
            "  onPressed: _refresh,\n"
            "  tooltip: 'Refresh',\n"
            "  icon: const Icon(Icons.refresh),\n"
            ")"
        ),
        min_version="Flutter 3.22",
        a11y="tooltip is REQUIRED for TalkBack — IconButton without it announces 'button' only.",
    ),
    _c(
        name="FloatingActionButton",
        platform="flutter",
        category="inputs",
        summary="Primary screen action FAB.",
        signature="FloatingActionButton(onPressed:, tooltip:, child:)",
        example=(
            "FloatingActionButton(\n"
            "  onPressed: _compose,\n"
            "  tooltip: 'Compose',\n"
            "  child: const Icon(Icons.edit),\n"
            ")"
        ),
        min_version="Flutter 3.22",
        variants=("FloatingActionButton.small", "FloatingActionButton.large", "FloatingActionButton.extended"),
    ),
    _c(
        name="TextField",
        platform="flutter",
        category="inputs",
        summary="Material text input; pair with TextEditingController + InputDecoration.",
        signature="TextField(controller:, decoration:, keyboardType:, obscureText:)",
        example=(
            "TextField(\n"
            "  controller: _emailCtrl,\n"
            "  keyboardType: TextInputType.emailAddress,\n"
            "  decoration: const InputDecoration(\n"
            "    labelText: 'Email',\n"
            "    helperText: 'We never share it.',\n"
            "    border: OutlineInputBorder(),\n"
            "  ),\n"
            ")"
        ),
        min_version="Flutter 3.22",
    ),
    _c(
        name="TextFormField",
        platform="flutter",
        category="inputs",
        summary="TextField + validator wired to a Form ancestor.",
        signature="TextFormField(controller:, decoration:, validator:)",
        example=(
            "TextFormField(\n"
            "  controller: _emailCtrl,\n"
            "  decoration: const InputDecoration(labelText: 'Email'),\n"
            "  validator: (v) => (v == null || !v.contains('@')) ? 'Invalid email' : null,\n"
            ")"
        ),
        min_version="Flutter 3.22",
    ),
    _c(
        name="Checkbox",
        platform="flutter",
        category="inputs",
        summary="Tri-state boolean checkbox (true / false / null indeterminate).",
        signature="Checkbox(value:, onChanged:, tristate:)",
        example='Checkbox(value: agreed, onChanged: (v) => setState(() => agreed = v ?? false))',
        min_version="Flutter 3.22",
        a11y="Pair with Text in a CheckboxListTile for one tappable label-and-control.",
    ),
    _c(
        name="Switch",
        platform="flutter",
        category="inputs",
        summary="Binary on/off switch (Material) — use CupertinoSwitch on iOS-flavor pages.",
        signature="Switch(value:, onChanged:)",
        example='Switch(value: darkMode, onChanged: (v) => setState(() => darkMode = v))',
        min_version="Flutter 3.22",
    ),
    _c(
        name="Slider",
        platform="flutter",
        category="inputs",
        summary="Continuous / discrete numeric slider.",
        signature="Slider(value:, onChanged:, min:, max:, divisions:, label:)",
        example=(
            "Slider(\n"
            "  value: volume,\n"
            "  min: 0,\n"
            "  max: 100,\n"
            "  divisions: 10,\n"
            "  label: volume.round().toString(),\n"
            "  onChanged: (v) => setState(() => volume = v),\n"
            ")"
        ),
        min_version="Flutter 3.22",
    ),
    _c(
        name="DropdownButton",
        platform="flutter",
        category="inputs",
        summary="Single-select dropdown; consider DropdownMenu (M3) for autocomplete.",
        signature="DropdownButton<T>(value:, items:, onChanged:)",
        example=(
            "DropdownButton<String>(\n"
            "  value: theme,\n"
            "  items: const [\n"
            "    DropdownMenuItem(value: 'light', child: Text('Light')),\n"
            "    DropdownMenuItem(value: 'dark', child: Text('Dark')),\n"
            "  ],\n"
            "  onChanged: (v) => setState(() => theme = v ?? theme),\n"
            ")"
        ),
        min_version="Flutter 3.22",
    ),
    # ── overlays ──────────────────────────────────────────────────
    _c(
        name="AlertDialog",
        platform="flutter",
        category="overlay",
        summary="Material 3 modal dialog (usually invoked via showDialog).",
        signature="showDialog(context:, builder:) => AlertDialog(title:, content:, actions:)",
        example=(
            "showDialog<void>(\n"
            "  context: context,\n"
            "  builder: (ctx) => AlertDialog(\n"
            "    title: const Text('Delete file?'),\n"
            "    content: const Text('This cannot be undone.'),\n"
            "    actions: [\n"
            "      TextButton(onPressed: () => Navigator.pop(ctx), child: const Text('Cancel')),\n"
            "      FilledButton(onPressed: () { _delete(); Navigator.pop(ctx); }, child: const Text('Delete')),\n"
            "    ],\n"
            "  ),\n"
            ")"
        ),
        min_version="Flutter 3.22",
        a11y="showDialog handles focus trap + barrier announcement automatically.",
    ),
    _c(
        name="showModalBottomSheet",
        platform="flutter",
        category="overlay",
        summary="Material modal bottom sheet (drag-dismiss, scrollable).",
        signature="showModalBottomSheet(context:, builder:, isScrollControlled:, useSafeArea:)",
        example=(
            "showModalBottomSheet<void>(\n"
            "  context: context,\n"
            "  isScrollControlled: true,\n"
            "  useSafeArea: true,\n"
            "  builder: (_) => const _FilterSheet(),\n"
            ")"
        ),
        min_version="Flutter 3.22",
    ),
    _c(
        name="DropdownMenu",
        platform="flutter",
        category="overlay",
        summary="M3 anchored dropdown with optional autocomplete.",
        signature="DropdownMenu<T>(initialSelection:, dropdownMenuEntries:, onSelected:)",
        example=(
            "DropdownMenu<String>(\n"
            "  initialSelection: 'system',\n"
            "  dropdownMenuEntries: const [\n"
            "    DropdownMenuEntry(value: 'system', label: 'System'),\n"
            "    DropdownMenuEntry(value: 'light', label: 'Light'),\n"
            "    DropdownMenuEntry(value: 'dark', label: 'Dark'),\n"
            "  ],\n"
            "  onSelected: (v) {/* ... */},\n"
            ")"
        ),
        min_version="Flutter 3.22",
    ),
    # ── feedback ──────────────────────────────────────────────────
    _c(
        name="SnackBar",
        platform="flutter",
        category="feedback",
        summary="Transient bottom feedback; show via ScaffoldMessenger.of(context).showSnackBar.",
        signature="ScaffoldMessenger.of(context).showSnackBar(SnackBar(content:, action:))",
        example=(
            "ScaffoldMessenger.of(context).showSnackBar(\n"
            "  SnackBar(\n"
            "    content: const Text('Saved'),\n"
            "    action: SnackBarAction(label: 'Undo', onPressed: _undo),\n"
            "  ),\n"
            ")"
        ),
        min_version="Flutter 3.22",
    ),
    _c(
        name="LinearProgressIndicator",
        platform="flutter",
        category="feedback",
        summary="Determinate / indeterminate horizontal progress bar.",
        signature="LinearProgressIndicator(value:, minHeight:)",
        example='LinearProgressIndicator(value: uploaded / total)',
        min_version="Flutter 3.22",
    ),
    _c(
        name="CircularProgressIndicator",
        platform="flutter",
        category="feedback",
        summary="Indeterminate / determinate circular spinner.",
        signature="CircularProgressIndicator(value:)",
        example="const CircularProgressIndicator()",
        min_version="Flutter 3.22",
    ),
    _c(
        name="RefreshIndicator",
        platform="flutter",
        category="feedback",
        summary="Pull-to-refresh wrapper around a scrollable.",
        signature="RefreshIndicator(onRefresh:, child:)",
        example=(
            "RefreshIndicator(\n"
            "  onRefresh: _reload,\n"
            "  child: ListView.builder(itemCount: items.length, itemBuilder: (_, i) => ItemTile(item: items[i])),\n"
            ")"
        ),
        min_version="Flutter 3.22",
        notes=("On iOS-flavor pages prefer CupertinoSliverRefreshControl inside a CustomScrollView.",),
    ),
    _c(
        name="Tooltip",
        platform="flutter",
        category="feedback",
        summary="Long-press / hover hint; never the only carrier of meaning on touch.",
        signature="Tooltip(message:, child:)",
        example='Tooltip(message: "Refresh", child: IconButton(onPressed: _reload, icon: const Icon(Icons.refresh)))',
        min_version="Flutter 3.22",
        a11y="Tooltip text is read by TalkBack as the icon's label — but mobile/touch users can't hover, so never rely on tooltip for required info.",
    ),
    # ── data ──────────────────────────────────────────────────────
    _c(
        name="Card",
        platform="flutter",
        category="data",
        summary="Material card surface with elevation + rounded shape.",
        signature="Card(child:, elevation:, margin:, shape:)",
        example=(
            "Card(\n"
            "  margin: const EdgeInsets.symmetric(horizontal: 16, vertical: 8),\n"
            "  child: Padding(\n"
            "    padding: const EdgeInsets.all(16),\n"
            "    child: Text('Title', style: theme.textTheme.titleMedium),\n"
            "  ),\n"
            ")"
        ),
        min_version="Flutter 3.22",
    ),
    _c(
        name="ListTile",
        platform="flutter",
        category="data",
        summary="List row with leading / title / subtitle / trailing slots.",
        signature="ListTile(leading:, title:, subtitle:, trailing:, onTap:)",
        example=(
            "ListTile(\n"
            "  leading: const Icon(Icons.person),\n"
            "  title: Text(message.subject),\n"
            "  subtitle: Text(message.preview, maxLines: 1, overflow: TextOverflow.ellipsis),\n"
            "  trailing: Text(message.timeAgo),\n"
            "  onTap: () => Navigator.push(context, MaterialPageRoute(builder: (_) => MessageDetail(id: message.id))),\n"
            ")"
        ),
        min_version="Flutter 3.22",
    ),
    _c(
        name="Icon",
        platform="flutter",
        category="data",
        summary="Material icon glyph (Icons.* set).",
        signature="Icon(IconData, color:, size:, semanticLabel:)",
        example='const Icon(Icons.settings, semanticLabel: "Settings")',
        min_version="Flutter 3.22",
        a11y="Decorative icons should set semanticLabel to null (or wrap in ExcludeSemantics).",
    ),
    _c(
        name="Text",
        platform="flutter",
        category="data",
        summary="Text widget; style via Theme.of(context).textTheme token.",
        signature="Text(data, style:, maxLines:, overflow:)",
        example='Text("Hello", style: Theme.of(context).textTheme.headlineSmall)',
        min_version="Flutter 3.22",
        notes=("Hard-coded fontSize breaks MediaQuery.textScalerOf — use the typography token.",),
    ),
)


# ── Combined registry ───────────────────────────────────────────────


_ENTRIES: tuple[MobileComponent, ...] = (*_SWIFTUI, *_COMPOSE, *_FLUTTER)

# Composite key (``"swiftui:NavigationStack"``) → entry.  Built once at
# import; immutable thereafter.
REGISTRY: dict[str, MobileComponent] = {c.key: c for c in _ENTRIES}


# ── Public API ──────────────────────────────────────────────────────


def _validate_platform(platform: str | None) -> None:
    if platform is not None and platform not in PLATFORMS:
        raise ValueError(
            f"Unknown platform {platform!r}; must be one of {PLATFORMS}"
        )


def _validate_category(category: str | None) -> None:
    if category is not None and category not in CATEGORIES:
        raise ValueError(
            f"Unknown category {category!r}; must be one of {CATEGORIES}"
        )


def _serialise(comp: MobileComponent) -> dict:
    """Convert a MobileComponent to a JSON-safe dict."""
    d = asdict(comp)
    d["variants"] = list(comp.variants)
    d["notes"] = list(comp.notes)
    d["deprecates"] = list(comp.deprecates)
    d["key"] = comp.key
    return d


def get_mobile_components(
    platform: str | None = None,
    category: str | None = None,
) -> list[dict]:
    """Return registry entries as JSON-serialisable dicts.

    The mobile-ui-designer agent's **step zero** — call this before
    emitting any Swift / Kotlin / Dart code so the agent picks
    components from current platform reality (not training memory).

    With no filters the full three-platform catalogue comes back,
    sorted by ``platform`` then ``name``.  ``platform=`` narrows to one
    target (used by the Edit auto-router for single-target prompts);
    ``category=`` narrows to one of :data:`CATEGORIES`.
    """
    _validate_platform(platform)
    _validate_category(category)
    out: list[dict] = []
    for comp in _sorted_entries():
        if platform is not None and comp.platform != platform:
            continue
        if category is not None and comp.category != category:
            continue
        out.append(_serialise(comp))
    return out


def get_component(platform: str, name: str) -> MobileComponent | None:
    """Return the registry entry for ``platform:name`` or None."""
    _validate_platform(platform)
    return REGISTRY.get(f"{platform}:{name}")


def list_component_names(platform: str | None = None) -> list[str]:
    """Return all registered component names, sorted by platform/name."""
    _validate_platform(platform)
    if platform is None:
        return [c.key for c in _sorted_entries()]
    return sorted(c.name for c in REGISTRY.values() if c.platform == platform)


def get_components_by_platform(platform: str) -> list[MobileComponent]:
    """Return every registry entry for one platform (sorted by name)."""
    _validate_platform(platform)
    return sorted(
        (c for c in REGISTRY.values() if c.platform == platform),
        key=lambda c: c.name,
    )


def get_components_by_category(
    category: str,
    platform: str | None = None,
) -> list[MobileComponent]:
    """Return entries with the given category (optionally filtered by platform)."""
    _validate_category(category)
    _validate_platform(platform)
    return [
        c
        for c in _sorted_entries()
        if c.category == category and (platform is None or c.platform == platform)
    ]


def render_agent_context_block(
    platforms: Iterable[str] | None = None,
    categories: Iterable[str] | None = None,
) -> str:
    """Render a compact markdown block for LLM context injection.

    Output is deterministic (sorted by platform → category → name) so
    the Anthropic prompt-cache key stays stable across runs.  Keep the
    rendering tight — the mobile-ui-designer skill is already ~400
    lines and the registry must fit alongside the design tokens block
    inside the agent's window.
    """
    plats = tuple(platforms) if platforms else PLATFORMS
    for p in plats:
        _validate_platform(p)
    cats = tuple(categories) if categories else CATEGORIES
    for c in cats:
        _validate_category(c)

    lines: list[str] = [
        f"# Mobile component registry (v{REGISTRY_SCHEMA_VERSION})",
        "",
        "Pick components from this list — never invent APIs from training "
        "memory.  All examples already use design tokens (no hard-coded hex / "
        "pt / dp / sp).",
        "",
    ]

    for plat in plats:
        plat_entries = [
            c for c in _sorted_entries()
            if c.platform == plat and c.category in cats
        ]
        if not plat_entries:
            continue
        lines.append(f"## {PLATFORM_LABELS[plat]}")
        lines.append("")
        by_cat: dict[str, list[MobileComponent]] = {c: [] for c in cats}
        for entry in plat_entries:
            by_cat[entry.category].append(entry)
        for cat in cats:
            entries = by_cat[cat]
            if not entries:
                continue
            lines.append(f"### {cat}")
            for entry in entries:
                lines.append(f"- **{entry.name}** — {entry.summary}")
                lines.append(f"  - signature: `{entry.signature}`")
                if entry.variants:
                    lines.append(
                        "  - variants: " + ", ".join(entry.variants)
                    )
                if entry.deprecates:
                    lines.append(
                        "  - replaces: " + ", ".join(entry.deprecates)
                    )
                lines.append(f"  - min: {entry.min_version}")
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _sorted_entries() -> list[MobileComponent]:
    """Stable platform-then-name ordering used by every public helper."""
    plat_order = {p: i for i, p in enumerate(PLATFORMS)}
    return sorted(
        REGISTRY.values(),
        key=lambda c: (plat_order[c.platform], c.name),
    )


__all__ = [
    "CATEGORIES",
    "PLATFORMS",
    "PLATFORM_LABELS",
    "REGISTRY",
    "REGISTRY_SCHEMA_VERSION",
    "MobileComponent",
    "get_component",
    "get_components_by_category",
    "get_components_by_platform",
    "get_mobile_components",
    "list_component_names",
    "render_agent_context_block",
]


if __name__ == "__main__":  # pragma: no cover
    print(json.dumps(get_mobile_components(), indent=2))
