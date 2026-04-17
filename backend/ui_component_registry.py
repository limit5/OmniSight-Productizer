"""V1 #2 (issue #317) — shadcn/ui component registry.

Canonical inventory of the shadcn/ui components installed under
``components/ui/``. The V1 **UI Designer** agent (see
``configs/roles/ui-designer.md``) MUST call ``get_available_components()``
before emitting any React + Tailwind code, so it drives element
selection off the real, currently-installed surface instead of
hallucinating a prop shape from its training memory.

Why a Python module instead of hand-written markdown inside the skill
file:

* The skill file (``configs/roles/ui-designer.md``) is immutable prompt
  context — it must stay compact.  The registry here is structured
  data (dataclasses → dicts → JSON), so it can be injected into the
  agent's context on-demand, sliced by category, or filtered by what
  is actually present on disk.
* A runtime check (:func:`get_available_components`) lets the tool
  whitelist adapt as shadcn updates land — new component added to
  ``components/ui/`` → show up in the returned list; component removed
  → filtered out automatically (never leaks a dangling API hint).
* The same structure feeds the sibling V1 workers
  (``backend/component_consistency_linter.py``,
  ``backend/vision_to_ui.py``, the Edit complexity auto-router) with
  a single source of truth.

Contract (pinned by ``backend/tests/test_ui_component_registry.py``):

* Every ``*.tsx`` under ``components/ui/`` that is a real component
  (i.e. not a utility hook like ``use-mobile.tsx``) has an entry in
  :data:`REGISTRY`.
* Every entry names at least one export, one canonical usage example,
  and a category from the fixed enum.
* :func:`get_available_components` returns JSON-serialisable dicts
  only (no dataclass instances leak across the tool boundary).
* :func:`render_agent_context_block` produces a compact markdown
  rendering suitable for LLM context injection (~ 1 token/prop).
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)

# Keep the version independent from individual component bumps —
# bump whenever the schema of a registry entry changes.
REGISTRY_SCHEMA_VERSION = "1.0.0"

# ── Fixed category taxonomy (mirrors the UI Designer skill) ──────────
# The linter and the Edit router depend on these exact strings.
CATEGORIES = (
    "inputs",        # Button, Input, Checkbox, …
    "form",          # Form / FormField composition
    "layout",        # Card, Sheet, Dialog, Sidebar, …
    "navigation",    # Tabs, Breadcrumb, Pagination, …
    "overlay",       # Popover, Tooltip, DropdownMenu, …
    "feedback",      # Alert, Toast, Progress, …
    "data",          # Table, Avatar, Badge, Chart, …
)

# Utility hooks/helpers in ``components/ui/`` that are NOT components.
# Keep explicit so on-disk scans don't raise "missing registry entry".
_UTILITY_FILES: frozenset[str] = frozenset({
    "use-mobile",
    "use-toast",
})


# ── Data model ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class ComponentProp:
    """One prop on a component API surface.

    ``type`` is the TypeScript type as the agent should write it
    (keep it short — ``"string"`` / ``"boolean"`` / ``"() => void"``).
    """

    name: str
    type: str
    default: str | None = None
    description: str = ""
    required: bool = False


@dataclass(frozen=True)
class ComponentVariant:
    """A CVA variant axis — e.g. ``variant: default|destructive|...``."""

    name: str
    values: tuple[str, ...]
    default: str | None = None


@dataclass(frozen=True)
class ShadcnComponent:
    """Registry entry for one shadcn/ui component.

    ``name`` is the filename stem under ``components/ui/`` and is the
    stable identifier used by the linter and the Edit router.
    """

    name: str
    category: str
    summary: str
    exports: tuple[str, ...]
    example: str
    props: tuple[ComponentProp, ...] = ()
    variants: tuple[ComponentVariant, ...] = ()
    aria_pattern: str | None = None
    notes: tuple[str, ...] = ()

    @property
    def import_path(self) -> str:
        return f"@/components/ui/{self.name}"

    def __post_init__(self) -> None:
        if self.category not in CATEGORIES:
            raise ValueError(
                f"Unknown category {self.category!r} for {self.name!r}; "
                f"must be one of {CATEGORIES}"
            )
        if not self.exports:
            raise ValueError(f"{self.name}: must declare at least one export")
        if not self.example.strip():
            raise ValueError(f"{self.name}: missing canonical example")


# ── Registry ────────────────────────────────────────────────────────
#
# Entries are intentionally terse but complete — the agent gets the
# names and shapes it needs to pick a component, not the full docs
# (those live on shadcn's site).  Examples are runnable TSX snippets
# using the project's ``@/`` path alias.


def _cmp(**kw) -> ShadcnComponent:
    return ShadcnComponent(**kw)


_ENTRIES: tuple[ShadcnComponent, ...] = (
    # ── inputs / actions ──────────────────────────────────────────
    _cmp(
        name="button",
        category="inputs",
        summary="Primary click target; use instead of raw <button>.",
        exports=("Button", "buttonVariants"),
        variants=(
            ComponentVariant(
                "variant",
                ("default", "destructive", "outline", "secondary", "ghost", "link"),
                default="default",
            ),
            ComponentVariant(
                "size",
                ("default", "sm", "lg", "icon", "icon-sm", "icon-lg"),
                default="default",
            ),
        ),
        props=(
            ComponentProp("asChild", "boolean", default="false",
                          description="Render as child via Radix Slot (e.g. wrap <Link>)."),
        ),
        example=(
            "<Button variant=\"default\" size=\"sm\" onClick={handle}>Save</Button>"
        ),
        aria_pattern="Button (APG)",
        notes=(
            "Icon-only buttons MUST set aria-label.",
            "Use asChild to turn Next.js <Link> into a button without nesting <a>.",
        ),
    ),
    _cmp(
        name="button-group",
        category="inputs",
        summary="Visually merged cluster of Buttons (shared borders).",
        exports=("ButtonGroup", "ButtonGroupSeparator", "ButtonGroupText"),
        example=(
            "<ButtonGroup>\n"
            "  <Button variant=\"outline\">Copy</Button>\n"
            "  <Button variant=\"outline\">Paste</Button>\n"
            "</ButtonGroup>"
        ),
    ),
    _cmp(
        name="input",
        category="inputs",
        summary="Single-line text input; drop-in for raw <input>.",
        exports=("Input",),
        props=(
            ComponentProp("type", "string", default='"text"'),
            ComponentProp("disabled", "boolean", default="false"),
        ),
        example='<Input type="email" placeholder="you@example.com" />',
        notes=(
            "Always pair with <Label htmlFor> or wrap in <Field>.",
            "Use aria-invalid on validation failure (shadcn styles the ring).",
        ),
    ),
    _cmp(
        name="textarea",
        category="inputs",
        summary="Multi-line text input; drop-in for raw <textarea>.",
        exports=("Textarea",),
        example='<Textarea placeholder="Notes" rows={4} />',
    ),
    _cmp(
        name="label",
        category="inputs",
        summary="Form label bound to a control via htmlFor.",
        exports=("Label",),
        example='<Label htmlFor="email">Email</Label>',
        notes=("htmlFor MUST match the control's id attribute.",),
    ),
    _cmp(
        name="input-group",
        category="inputs",
        summary="Input + leading/trailing addon (icon, text, button).",
        exports=(
            "InputGroup",
            "InputGroupAddon",
            "InputGroupButton",
            "InputGroupInput",
            "InputGroupText",
            "InputGroupTextarea",
        ),
        example=(
            "<InputGroup>\n"
            "  <InputGroupAddon><SearchIcon /></InputGroupAddon>\n"
            "  <InputGroupInput placeholder=\"Search…\" />\n"
            "</InputGroup>"
        ),
    ),
    _cmp(
        name="input-otp",
        category="inputs",
        summary="One-time-password entry (4–8 segmented digits).",
        exports=("InputOTP", "InputOTPGroup", "InputOTPSlot", "InputOTPSeparator"),
        example=(
            "<InputOTP maxLength={6}>\n"
            "  <InputOTPGroup>\n"
            "    {Array.from({length:6}).map((_,i) => <InputOTPSlot index={i} key={i} />)}\n"
            "  </InputOTPGroup>\n"
            "</InputOTP>"
        ),
    ),
    _cmp(
        name="field",
        category="inputs",
        summary="Unified form-field wrapper (label / control / hint / error).",
        exports=(
            "Field",
            "FieldLabel",
            "FieldControl",
            "FieldDescription",
            "FieldError",
            "FieldSet",
            "FieldLegend",
        ),
        example=(
            "<Field>\n"
            "  <FieldLabel>Email</FieldLabel>\n"
            "  <FieldControl><Input type=\"email\" /></FieldControl>\n"
            "  <FieldDescription>We never share it.</FieldDescription>\n"
            "  <FieldError />\n"
            "</Field>"
        ),
        aria_pattern="Form Field (WAI-ARIA APG)",
    ),
    _cmp(
        name="checkbox",
        category="inputs",
        summary="Boolean toggle; Radix checkbox.",
        exports=("Checkbox",),
        props=(
            ComponentProp("checked", "boolean | 'indeterminate'"),
            ComponentProp("onCheckedChange", "(checked: boolean) => void"),
            ComponentProp("disabled", "boolean", default="false"),
        ),
        example='<Checkbox id="terms" /> <Label htmlFor="terms">Accept ToS</Label>',
        aria_pattern="Checkbox (APG)",
    ),
    _cmp(
        name="radio-group",
        category="inputs",
        summary="Single-choice radio set; Radix radio group.",
        exports=("RadioGroup", "RadioGroupItem"),
        example=(
            "<RadioGroup defaultValue=\"monthly\">\n"
            "  <RadioGroupItem value=\"monthly\" id=\"m\" />\n"
            "  <Label htmlFor=\"m\">Monthly</Label>\n"
            "</RadioGroup>"
        ),
        aria_pattern="Radio Group (APG)",
    ),
    _cmp(
        name="switch",
        category="inputs",
        summary="Binary on/off switch; prefer over Checkbox for settings.",
        exports=("Switch",),
        props=(
            ComponentProp("checked", "boolean"),
            ComponentProp("onCheckedChange", "(checked: boolean) => void"),
        ),
        example='<Switch id="notify" /> <Label htmlFor="notify">Email me</Label>',
        aria_pattern="Switch (APG)",
    ),
    _cmp(
        name="slider",
        category="inputs",
        summary="Numeric range / value slider; Radix slider.",
        exports=("Slider",),
        props=(
            ComponentProp("defaultValue", "number[]"),
            ComponentProp("min", "number", default="0"),
            ComponentProp("max", "number", default="100"),
            ComponentProp("step", "number", default="1"),
        ),
        example='<Slider defaultValue={[50]} max={100} step={1} />',
        aria_pattern="Slider (APG)",
    ),
    _cmp(
        name="toggle",
        category="inputs",
        summary="Two-state pressable toggle (e.g. bold / italic).",
        exports=("Toggle", "toggleVariants"),
        variants=(
            ComponentVariant("variant", ("default", "outline"), default="default"),
            ComponentVariant("size", ("default", "sm", "lg"), default="default"),
        ),
        example='<Toggle aria-label="Bold"><BoldIcon /></Toggle>',
    ),
    _cmp(
        name="toggle-group",
        category="inputs",
        summary="Single- or multi-select group of Toggles.",
        exports=("ToggleGroup", "ToggleGroupItem"),
        props=(
            ComponentProp("type", '"single" | "multiple"', required=True),
            ComponentProp("value", "string | string[]"),
        ),
        example=(
            "<ToggleGroup type=\"single\" defaultValue=\"left\">\n"
            "  <ToggleGroupItem value=\"left\">L</ToggleGroupItem>\n"
            "  <ToggleGroupItem value=\"center\">C</ToggleGroupItem>\n"
            "</ToggleGroup>"
        ),
    ),
    _cmp(
        name="select",
        category="inputs",
        summary="Single-select dropdown; Radix listbox.",
        exports=(
            "Select",
            "SelectGroup",
            "SelectValue",
            "SelectTrigger",
            "SelectContent",
            "SelectLabel",
            "SelectItem",
            "SelectSeparator",
            "SelectScrollUpButton",
            "SelectScrollDownButton",
        ),
        example=(
            "<Select>\n"
            "  <SelectTrigger><SelectValue placeholder=\"Pick\" /></SelectTrigger>\n"
            "  <SelectContent>\n"
            "    <SelectItem value=\"a\">Alpha</SelectItem>\n"
            "    <SelectItem value=\"b\">Beta</SelectItem>\n"
            "  </SelectContent>\n"
            "</Select>"
        ),
        aria_pattern="Listbox (APG)",
        notes=("For combobox (typeahead), use Command + Popover instead.",),
    ),
    _cmp(
        name="calendar",
        category="inputs",
        summary="Date / date-range picker grid (react-day-picker v9).",
        exports=("Calendar", "CalendarDayButton"),
        props=(
            ComponentProp("mode", '"single" | "multiple" | "range"', default='"single"'),
            ComponentProp("selected", "Date | Date[] | DateRange"),
            ComponentProp("onSelect", "(date: Date | undefined) => void"),
        ),
        example='<Calendar mode="single" selected={date} onSelect={setDate} />',
        notes=("Pair with Popover for a DatePicker.",),
    ),
    # ── form composition ──────────────────────────────────────────
    _cmp(
        name="form",
        category="form",
        summary="react-hook-form + zod adapter; use with useForm().",
        exports=(
            "useFormField",
            "Form",
            "FormItem",
            "FormLabel",
            "FormControl",
            "FormDescription",
            "FormMessage",
            "FormField",
        ),
        example=(
            "const form = useForm({ resolver: zodResolver(schema) })\n"
            "<Form {...form}>\n"
            "  <form onSubmit={form.handleSubmit(onValid)}>\n"
            "    <FormField control={form.control} name=\"email\" render={({field}) => (\n"
            "      <FormItem>\n"
            "        <FormLabel>Email</FormLabel>\n"
            "        <FormControl><Input {...field} /></FormControl>\n"
            "        <FormMessage />\n"
            "      </FormItem>\n"
            "    )} />\n"
            "  </form>\n"
            "</Form>"
        ),
        notes=(
            "FormMessage auto-wires aria-describedby + role=alert.",
            "Never skip FormControl — it forwards the id + aria-invalid.",
        ),
    ),
    # ── layout / containers ───────────────────────────────────────
    _cmp(
        name="card",
        category="layout",
        summary="Panel container (header / body / footer).",
        exports=(
            "Card",
            "CardHeader",
            "CardTitle",
            "CardDescription",
            "CardContent",
            "CardFooter",
            "CardAction",
        ),
        example=(
            "<Card>\n"
            "  <CardHeader>\n"
            "    <CardTitle>Plan</CardTitle>\n"
            "    <CardDescription>Billed monthly</CardDescription>\n"
            "  </CardHeader>\n"
            "  <CardContent>…</CardContent>\n"
            "  <CardFooter><Button>Upgrade</Button></CardFooter>\n"
            "</Card>"
        ),
    ),
    _cmp(
        name="sheet",
        category="layout",
        summary="Side-drawer modal (left / right / top / bottom).",
        exports=(
            "Sheet",
            "SheetPortal",
            "SheetOverlay",
            "SheetTrigger",
            "SheetClose",
            "SheetContent",
            "SheetHeader",
            "SheetFooter",
            "SheetTitle",
            "SheetDescription",
        ),
        props=(
            ComponentProp("side", '"top" | "right" | "bottom" | "left"', default='"right"'),
        ),
        example=(
            "<Sheet>\n"
            "  <SheetTrigger asChild><Button>Open</Button></SheetTrigger>\n"
            "  <SheetContent side=\"right\">\n"
            "    <SheetHeader><SheetTitle>Settings</SheetTitle></SheetHeader>\n"
            "  </SheetContent>\n"
            "</Sheet>"
        ),
        aria_pattern="Dialog (Modal) (APG)",
    ),
    _cmp(
        name="drawer",
        category="layout",
        summary="Mobile bottom drawer (Vaul; drag-to-dismiss).",
        exports=(
            "Drawer",
            "DrawerPortal",
            "DrawerOverlay",
            "DrawerTrigger",
            "DrawerClose",
            "DrawerContent",
            "DrawerHeader",
            "DrawerFooter",
            "DrawerTitle",
            "DrawerDescription",
        ),
        example=(
            "<Drawer>\n"
            "  <DrawerTrigger asChild><Button>Open</Button></DrawerTrigger>\n"
            "  <DrawerContent><DrawerHeader><DrawerTitle>…</DrawerTitle></DrawerHeader></DrawerContent>\n"
            "</Drawer>"
        ),
        aria_pattern="Dialog (Modal) (APG)",
    ),
    _cmp(
        name="dialog",
        category="layout",
        summary="Centered modal with focus trap + ESC + scroll lock.",
        exports=(
            "Dialog",
            "DialogPortal",
            "DialogOverlay",
            "DialogTrigger",
            "DialogClose",
            "DialogContent",
            "DialogHeader",
            "DialogFooter",
            "DialogTitle",
            "DialogDescription",
        ),
        example=(
            "<Dialog>\n"
            "  <DialogTrigger asChild><Button>Edit</Button></DialogTrigger>\n"
            "  <DialogContent>\n"
            "    <DialogHeader><DialogTitle>Edit profile</DialogTitle></DialogHeader>\n"
            "  </DialogContent>\n"
            "</Dialog>"
        ),
        aria_pattern="Dialog (Modal) (APG)",
        notes=("DialogTitle is required (use VisuallyHidden if you need it hidden).",),
    ),
    _cmp(
        name="alert-dialog",
        category="layout",
        summary="Destructive-action confirmation modal (cannot ESC / click-out).",
        exports=(
            "AlertDialog",
            "AlertDialogPortal",
            "AlertDialogOverlay",
            "AlertDialogTrigger",
            "AlertDialogContent",
            "AlertDialogHeader",
            "AlertDialogFooter",
            "AlertDialogTitle",
            "AlertDialogDescription",
            "AlertDialogAction",
            "AlertDialogCancel",
        ),
        example=(
            "<AlertDialog>\n"
            "  <AlertDialogTrigger asChild><Button variant=\"destructive\">Delete</Button></AlertDialogTrigger>\n"
            "  <AlertDialogContent>\n"
            "    <AlertDialogHeader><AlertDialogTitle>Delete?</AlertDialogTitle></AlertDialogHeader>\n"
            "    <AlertDialogFooter>\n"
            "      <AlertDialogCancel>Cancel</AlertDialogCancel>\n"
            "      <AlertDialogAction>Confirm</AlertDialogAction>\n"
            "    </AlertDialogFooter>\n"
            "  </AlertDialogContent>\n"
            "</AlertDialog>"
        ),
        aria_pattern="AlertDialog (APG)",
    ),
    _cmp(
        name="sidebar",
        category="layout",
        summary="App-shell sidebar with collapse + icon-only state.",
        exports=(
            "Sidebar",
            "SidebarProvider",
            "SidebarTrigger",
            "SidebarRail",
            "SidebarInset",
            "SidebarInput",
            "SidebarHeader",
            "SidebarFooter",
            "SidebarSeparator",
            "SidebarContent",
            "SidebarGroup",
            "SidebarGroupLabel",
            "SidebarGroupAction",
            "SidebarGroupContent",
            "SidebarMenu",
            "SidebarMenuItem",
            "SidebarMenuButton",
            "SidebarMenuAction",
            "SidebarMenuBadge",
            "SidebarMenuSkeleton",
            "SidebarMenuSub",
            "SidebarMenuSubItem",
            "SidebarMenuSubButton",
            "useSidebar",
        ),
        example=(
            "<SidebarProvider>\n"
            "  <Sidebar>\n"
            "    <SidebarContent>\n"
            "      <SidebarMenu>\n"
            "        <SidebarMenuItem><SidebarMenuButton>Inbox</SidebarMenuButton></SidebarMenuItem>\n"
            "      </SidebarMenu>\n"
            "    </SidebarContent>\n"
            "  </Sidebar>\n"
            "  <SidebarInset>{children}</SidebarInset>\n"
            "</SidebarProvider>"
        ),
        notes=(
            "Collapse state lives in the SidebarProvider — never toggle with lg:hidden.",
            "Use useSidebar() to read state from nested components.",
        ),
    ),
    _cmp(
        name="resizable",
        category="layout",
        summary="Two- or three-pane resizable split (react-resizable-panels).",
        exports=("ResizablePanelGroup", "ResizablePanel", "ResizableHandle"),
        example=(
            "<ResizablePanelGroup direction=\"horizontal\">\n"
            "  <ResizablePanel defaultSize={30}>left</ResizablePanel>\n"
            "  <ResizableHandle />\n"
            "  <ResizablePanel>right</ResizablePanel>\n"
            "</ResizablePanelGroup>"
        ),
    ),
    _cmp(
        name="scroll-area",
        category="layout",
        summary="Custom-styled scroll container that preserves native behavior.",
        exports=("ScrollArea", "ScrollBar"),
        example=(
            "<ScrollArea className=\"h-64\">\n"
            "  {items.map(i => <div key={i}>{i}</div>)}\n"
            "</ScrollArea>"
        ),
    ),
    _cmp(
        name="separator",
        category="layout",
        summary="Horizontal / vertical divider.",
        exports=("Separator",),
        props=(
            ComponentProp("orientation", '"horizontal" | "vertical"', default='"horizontal"'),
            ComponentProp("decorative", "boolean", default="true",
                          description="If false, adds role=separator for AT."),
        ),
        example='<Separator orientation="horizontal" />',
    ),
    _cmp(
        name="aspect-ratio",
        category="layout",
        summary="Constrains children to a fixed ratio (e.g. 16/9).",
        exports=("AspectRatio",),
        props=(ComponentProp("ratio", "number", default="1"),),
        example='<AspectRatio ratio={16/9}><img src={src} alt="" /></AspectRatio>',
    ),
    _cmp(
        name="collapsible",
        category="layout",
        summary="Single disclosure region (trigger + content).",
        exports=("Collapsible", "CollapsibleTrigger", "CollapsibleContent"),
        example=(
            "<Collapsible>\n"
            "  <CollapsibleTrigger>Toggle</CollapsibleTrigger>\n"
            "  <CollapsibleContent>Body</CollapsibleContent>\n"
            "</Collapsible>"
        ),
        aria_pattern="Disclosure (APG)",
    ),
    _cmp(
        name="accordion",
        category="layout",
        summary="Multiple disclosure regions (FAQ pattern).",
        exports=("Accordion", "AccordionItem", "AccordionTrigger", "AccordionContent"),
        props=(
            ComponentProp("type", '"single" | "multiple"', required=True),
            ComponentProp("collapsible", "boolean", default="false",
                          description="If type=single, allow closing the open item."),
        ),
        example=(
            "<Accordion type=\"single\" collapsible>\n"
            "  <AccordionItem value=\"q1\">\n"
            "    <AccordionTrigger>Q1?</AccordionTrigger>\n"
            "    <AccordionContent>A1.</AccordionContent>\n"
            "  </AccordionItem>\n"
            "</Accordion>"
        ),
        aria_pattern="Accordion (APG)",
    ),
    # ── navigation ────────────────────────────────────────────────
    _cmp(
        name="tabs",
        category="navigation",
        summary="Tablist + panels; Radix tabs.",
        exports=("Tabs", "TabsList", "TabsTrigger", "TabsContent"),
        props=(
            ComponentProp("defaultValue", "string"),
            ComponentProp("value", "string", description="Controlled value."),
            ComponentProp("onValueChange", "(value: string) => void"),
        ),
        example=(
            "<Tabs defaultValue=\"monthly\">\n"
            "  <TabsList>\n"
            "    <TabsTrigger value=\"monthly\">Monthly</TabsTrigger>\n"
            "    <TabsTrigger value=\"yearly\">Yearly</TabsTrigger>\n"
            "  </TabsList>\n"
            "  <TabsContent value=\"monthly\">…</TabsContent>\n"
            "  <TabsContent value=\"yearly\">…</TabsContent>\n"
            "</Tabs>"
        ),
        aria_pattern="Tabs (APG)",
    ),
    _cmp(
        name="navigation-menu",
        category="navigation",
        summary="Mega-menu / top-bar navigation (Radix NavigationMenu).",
        exports=(
            "NavigationMenu",
            "NavigationMenuList",
            "NavigationMenuItem",
            "NavigationMenuContent",
            "NavigationMenuTrigger",
            "NavigationMenuLink",
            "NavigationMenuIndicator",
            "NavigationMenuViewport",
            "navigationMenuTriggerStyle",
        ),
        example=(
            "<NavigationMenu>\n"
            "  <NavigationMenuList>\n"
            "    <NavigationMenuItem>\n"
            "      <NavigationMenuTrigger>Products</NavigationMenuTrigger>\n"
            "      <NavigationMenuContent>…</NavigationMenuContent>\n"
            "    </NavigationMenuItem>\n"
            "  </NavigationMenuList>\n"
            "</NavigationMenu>"
        ),
    ),
    _cmp(
        name="menubar",
        category="navigation",
        summary="Desktop-app-style menu bar (File / Edit / …).",
        exports=(
            "Menubar",
            "MenubarMenu",
            "MenubarTrigger",
            "MenubarContent",
            "MenubarItem",
            "MenubarSeparator",
            "MenubarLabel",
            "MenubarCheckboxItem",
            "MenubarRadioGroup",
            "MenubarRadioItem",
            "MenubarPortal",
            "MenubarSubContent",
            "MenubarSubTrigger",
            "MenubarGroup",
            "MenubarSub",
            "MenubarShortcut",
        ),
        example=(
            "<Menubar>\n"
            "  <MenubarMenu>\n"
            "    <MenubarTrigger>File</MenubarTrigger>\n"
            "    <MenubarContent><MenubarItem>New</MenubarItem></MenubarContent>\n"
            "  </MenubarMenu>\n"
            "</Menubar>"
        ),
    ),
    _cmp(
        name="breadcrumb",
        category="navigation",
        summary="Trail-style navigation (Home › Docs › Page).",
        exports=(
            "Breadcrumb",
            "BreadcrumbList",
            "BreadcrumbItem",
            "BreadcrumbLink",
            "BreadcrumbPage",
            "BreadcrumbSeparator",
            "BreadcrumbEllipsis",
        ),
        example=(
            "<Breadcrumb>\n"
            "  <BreadcrumbList>\n"
            "    <BreadcrumbItem><BreadcrumbLink href=\"/\">Home</BreadcrumbLink></BreadcrumbItem>\n"
            "    <BreadcrumbSeparator />\n"
            "    <BreadcrumbItem><BreadcrumbPage>Current</BreadcrumbPage></BreadcrumbItem>\n"
            "  </BreadcrumbList>\n"
            "</Breadcrumb>"
        ),
        notes=("BreadcrumbPage is the current page — NOT a link.",),
    ),
    _cmp(
        name="pagination",
        category="navigation",
        summary="Page list + prev/next for paged content.",
        exports=(
            "Pagination",
            "PaginationContent",
            "PaginationLink",
            "PaginationItem",
            "PaginationPrevious",
            "PaginationNext",
            "PaginationEllipsis",
        ),
        example=(
            "<Pagination>\n"
            "  <PaginationContent>\n"
            "    <PaginationItem><PaginationPrevious href=\"#\" /></PaginationItem>\n"
            "    <PaginationItem><PaginationLink href=\"#\" isActive>1</PaginationLink></PaginationItem>\n"
            "    <PaginationItem><PaginationNext href=\"#\" /></PaginationItem>\n"
            "  </PaginationContent>\n"
            "</Pagination>"
        ),
    ),
    _cmp(
        name="command",
        category="navigation",
        summary="Command palette / fuzzy picker (cmdk).",
        exports=(
            "Command",
            "CommandDialog",
            "CommandInput",
            "CommandList",
            "CommandEmpty",
            "CommandGroup",
            "CommandItem",
            "CommandShortcut",
            "CommandSeparator",
        ),
        example=(
            "<Command>\n"
            "  <CommandInput placeholder=\"Search…\" />\n"
            "  <CommandList>\n"
            "    <CommandEmpty>No results.</CommandEmpty>\n"
            "    <CommandGroup heading=\"Pages\">\n"
            "      <CommandItem>Dashboard</CommandItem>\n"
            "    </CommandGroup>\n"
            "  </CommandList>\n"
            "</Command>"
        ),
        aria_pattern="Combobox (APG)",
        notes=("Wrap in Popover for a typeahead Combobox pattern.",),
    ),
    # ── overlays ──────────────────────────────────────────────────
    _cmp(
        name="popover",
        category="overlay",
        summary="Anchored floating panel (non-modal).",
        exports=("Popover", "PopoverTrigger", "PopoverContent", "PopoverAnchor"),
        example=(
            "<Popover>\n"
            "  <PopoverTrigger asChild><Button>Open</Button></PopoverTrigger>\n"
            "  <PopoverContent>…</PopoverContent>\n"
            "</Popover>"
        ),
    ),
    _cmp(
        name="hover-card",
        category="overlay",
        summary="Preview card revealed on pointer hover / focus.",
        exports=("HoverCard", "HoverCardTrigger", "HoverCardContent"),
        example=(
            "<HoverCard>\n"
            "  <HoverCardTrigger asChild><a href=\"/u\">@octocat</a></HoverCardTrigger>\n"
            "  <HoverCardContent>Bio…</HoverCardContent>\n"
            "</HoverCard>"
        ),
        notes=("Not reachable via touch — never put critical info here.",),
    ),
    _cmp(
        name="tooltip",
        category="overlay",
        summary="Short hint on hover/focus; needs <TooltipProvider>.",
        exports=("Tooltip", "TooltipTrigger", "TooltipContent", "TooltipProvider"),
        example=(
            "<TooltipProvider>\n"
            "  <Tooltip>\n"
            "    <TooltipTrigger asChild><Button variant=\"ghost\" size=\"icon\"><InfoIcon /></Button></TooltipTrigger>\n"
            "    <TooltipContent>Docs</TooltipContent>\n"
            "  </Tooltip>\n"
            "</TooltipProvider>"
        ),
        aria_pattern="Tooltip (APG)",
        notes=(
            "Unreachable on mobile/touch — never use for the only copy of info.",
            "Mount TooltipProvider once near the root (e.g. in layout).",
        ),
    ),
    _cmp(
        name="dropdown-menu",
        category="overlay",
        summary="Action menu triggered by a button click.",
        exports=(
            "DropdownMenu",
            "DropdownMenuTrigger",
            "DropdownMenuContent",
            "DropdownMenuItem",
            "DropdownMenuCheckboxItem",
            "DropdownMenuRadioItem",
            "DropdownMenuLabel",
            "DropdownMenuSeparator",
            "DropdownMenuShortcut",
            "DropdownMenuGroup",
            "DropdownMenuPortal",
            "DropdownMenuSub",
            "DropdownMenuSubContent",
            "DropdownMenuSubTrigger",
            "DropdownMenuRadioGroup",
        ),
        example=(
            "<DropdownMenu>\n"
            "  <DropdownMenuTrigger asChild><Button>Actions</Button></DropdownMenuTrigger>\n"
            "  <DropdownMenuContent>\n"
            "    <DropdownMenuItem>Edit</DropdownMenuItem>\n"
            "    <DropdownMenuItem>Delete</DropdownMenuItem>\n"
            "  </DropdownMenuContent>\n"
            "</DropdownMenu>"
        ),
        aria_pattern="Menu (APG)",
    ),
    _cmp(
        name="context-menu",
        category="overlay",
        summary="Right-click menu (desktop) / long-press (touch).",
        exports=(
            "ContextMenu",
            "ContextMenuTrigger",
            "ContextMenuContent",
            "ContextMenuItem",
            "ContextMenuCheckboxItem",
            "ContextMenuRadioItem",
            "ContextMenuLabel",
            "ContextMenuSeparator",
            "ContextMenuShortcut",
            "ContextMenuGroup",
            "ContextMenuPortal",
            "ContextMenuSub",
            "ContextMenuSubContent",
            "ContextMenuSubTrigger",
            "ContextMenuRadioGroup",
        ),
        example=(
            "<ContextMenu>\n"
            "  <ContextMenuTrigger>Right-click me</ContextMenuTrigger>\n"
            "  <ContextMenuContent>\n"
            "    <ContextMenuItem>Copy</ContextMenuItem>\n"
            "  </ContextMenuContent>\n"
            "</ContextMenu>"
        ),
        aria_pattern="Menu (APG)",
    ),
    # ── feedback ──────────────────────────────────────────────────
    _cmp(
        name="alert",
        category="feedback",
        summary="Inline banner with title + description (info / destructive).",
        exports=("Alert", "AlertTitle", "AlertDescription"),
        variants=(
            ComponentVariant("variant", ("default", "destructive"), default="default"),
        ),
        example=(
            "<Alert variant=\"destructive\">\n"
            "  <AlertTitle>Error</AlertTitle>\n"
            "  <AlertDescription>Could not save.</AlertDescription>\n"
            "</Alert>"
        ),
        aria_pattern="Alert (APG)",
    ),
    _cmp(
        name="progress",
        category="feedback",
        summary="Determinate horizontal progress bar (0–100).",
        exports=("Progress",),
        props=(ComponentProp("value", "number"),),
        example='<Progress value={60} />',
        notes=("Use Skeleton for unknown-duration loading instead.",),
    ),
    _cmp(
        name="skeleton",
        category="feedback",
        summary="Placeholder shimmer in the shape of the real content.",
        exports=("Skeleton",),
        example='<Skeleton className="h-4 w-48" />',
    ),
    _cmp(
        name="spinner",
        category="feedback",
        summary="Indeterminate rotating loader; role=status.",
        exports=("Spinner",),
        example='<Spinner />',
    ),
    _cmp(
        name="toast",
        category="feedback",
        summary="Transient notification primitives (paired with Toaster / useToast).",
        exports=(
            "Toast",
            "ToastAction",
            "ToastClose",
            "ToastDescription",
            "ToastProvider",
            "ToastTitle",
            "ToastViewport",
        ),
        example=(
            "const { toast } = useToast()\n"
            "toast({ title: \"Saved\", description: \"Changes persisted.\" })"
        ),
    ),
    _cmp(
        name="toaster",
        category="feedback",
        summary="Mount-once root for the Toast notifications stack.",
        exports=("Toaster",),
        example='<Toaster />  /* mount once in app/layout.tsx */',
    ),
    _cmp(
        name="sonner",
        category="feedback",
        summary="Alternative toast stack (sonner lib); opinionated UX.",
        exports=("Toaster",),
        example=(
            "import { toast } from \"sonner\"\n"
            "<Toaster richColors position=\"top-right\" />  /* mount once */\n"
            "toast.success(\"Saved\")"
        ),
    ),
    # ── data display ──────────────────────────────────────────────
    _cmp(
        name="table",
        category="data",
        summary="Semantic data table (pair with @tanstack/react-table for ≥50 rows).",
        exports=(
            "Table",
            "TableHeader",
            "TableBody",
            "TableFooter",
            "TableHead",
            "TableRow",
            "TableCell",
            "TableCaption",
        ),
        example=(
            "<Table>\n"
            "  <TableCaption>Invoices</TableCaption>\n"
            "  <TableHeader>\n"
            "    <TableRow><TableHead>Id</TableHead><TableHead>Total</TableHead></TableRow>\n"
            "  </TableHeader>\n"
            "  <TableBody>\n"
            "    <TableRow><TableCell>INV-01</TableCell><TableCell>$19</TableCell></TableRow>\n"
            "  </TableBody>\n"
            "</Table>"
        ),
        aria_pattern="Table (APG)",
    ),
    _cmp(
        name="avatar",
        category="data",
        summary="Circular user image with fallback initials.",
        exports=("Avatar", "AvatarImage", "AvatarFallback"),
        example=(
            "<Avatar>\n"
            "  <AvatarImage src={url} alt={name} />\n"
            "  <AvatarFallback>AB</AvatarFallback>\n"
            "</Avatar>"
        ),
    ),
    _cmp(
        name="badge",
        category="data",
        summary="Small status / count pill.",
        exports=("Badge", "badgeVariants"),
        variants=(
            ComponentVariant(
                "variant",
                ("default", "secondary", "destructive", "outline"),
                default="default",
            ),
        ),
        example='<Badge variant="secondary">New</Badge>',
    ),
    _cmp(
        name="kbd",
        category="data",
        summary="Keyboard shortcut glyphs (⌘K).",
        exports=("Kbd", "KbdGroup"),
        example='<Kbd>⌘</Kbd> <Kbd>K</Kbd>',
    ),
    _cmp(
        name="carousel",
        category="data",
        summary="Horizontal slideshow (Embla).",
        exports=(
            "Carousel",
            "CarouselContent",
            "CarouselItem",
            "CarouselPrevious",
            "CarouselNext",
            "useCarousel",
        ),
        example=(
            "<Carousel>\n"
            "  <CarouselContent>\n"
            "    <CarouselItem>1</CarouselItem>\n"
            "    <CarouselItem>2</CarouselItem>\n"
            "  </CarouselContent>\n"
            "  <CarouselPrevious />\n"
            "  <CarouselNext />\n"
            "</Carousel>"
        ),
        notes=(
            "If auto-play is on, add a Pause control (WCAG 2.2.2).",
        ),
    ),
    _cmp(
        name="chart",
        category="data",
        summary="Recharts wrapper with design-token color slots (--chart-1..5).",
        exports=(
            "ChartContainer",
            "ChartTooltip",
            "ChartTooltipContent",
            "ChartLegend",
            "ChartLegendContent",
            "ChartStyle",
        ),
        example=(
            "const config = { revenue: { label: \"Revenue\", color: \"var(--chart-1)\" } }\n"
            "<ChartContainer config={config}>\n"
            "  <LineChart data={data}>\n"
            "    <ChartTooltip content={<ChartTooltipContent />} />\n"
            "  </LineChart>\n"
            "</ChartContainer>"
        ),
        notes=("Colors flow through config — do NOT hard-code stroke/fill hexes.",),
    ),
    _cmp(
        name="empty",
        category="data",
        summary="Empty-state container (media + title + description + CTA).",
        exports=(
            "Empty",
            "EmptyHeader",
            "EmptyMedia",
            "EmptyTitle",
            "EmptyDescription",
            "EmptyContent",
        ),
        example=(
            "<Empty>\n"
            "  <EmptyHeader>\n"
            "    <EmptyMedia><InboxIcon /></EmptyMedia>\n"
            "    <EmptyTitle>No messages</EmptyTitle>\n"
            "    <EmptyDescription>You're all caught up.</EmptyDescription>\n"
            "  </EmptyHeader>\n"
            "  <EmptyContent><Button>Refresh</Button></EmptyContent>\n"
            "</Empty>"
        ),
    ),
    _cmp(
        name="item",
        category="data",
        summary="Generic list row (icon + title + description + action).",
        exports=(
            "Item",
            "ItemGroup",
            "ItemMedia",
            "ItemContent",
            "ItemTitle",
            "ItemDescription",
            "ItemActions",
            "ItemHeader",
            "ItemFooter",
            "ItemSeparator",
        ),
        example=(
            "<Item>\n"
            "  <ItemMedia><FileIcon /></ItemMedia>\n"
            "  <ItemContent>\n"
            "    <ItemTitle>README.md</ItemTitle>\n"
            "    <ItemDescription>12 KB</ItemDescription>\n"
            "  </ItemContent>\n"
            "  <ItemActions><Button variant=\"ghost\" size=\"icon-sm\">…</Button></ItemActions>\n"
            "</Item>"
        ),
    ),
)


# Build once at import — immutable.
REGISTRY: dict[str, ShadcnComponent] = {c.name: c for c in _ENTRIES}


# ── Public API ──────────────────────────────────────────────────────


_DEFAULT_UI_DIR = Path(__file__).resolve().parent.parent / "components" / "ui"


def _scan_installed(ui_dir: Path) -> set[str]:
    """Return the set of installed component stems under ``ui_dir``.

    Silently returns an empty set if the directory does not exist
    (e.g. called from a pure-backend clone of the repo without the
    frontend tree).
    """
    if not ui_dir.is_dir():
        return set()
    installed: set[str] = set()
    for entry in ui_dir.iterdir():
        if entry.suffix != ".tsx" or not entry.is_file():
            continue
        stem = entry.stem
        if stem in _UTILITY_FILES:
            continue
        installed.add(stem)
    return installed


def get_available_components(
    project_root: Path | None = None,
    category: str | None = None,
) -> list[dict]:
    """Return every registered shadcn component as a JSON-serialisable dict.

    When ``project_root`` is supplied (e.g. a target repo under
    ``/workspace/…``), the registry is filtered to components whose
    ``.tsx`` file is actually present on disk.  When omitted, the
    registry is unfiltered — callers like tests can inspect the
    canonical catalogue.

    ``category`` further narrows the result to one of :data:`CATEGORIES`.
    """
    if category is not None and category not in CATEGORIES:
        raise ValueError(
            f"Unknown category {category!r}; must be one of {CATEGORIES}"
        )

    if project_root is not None:
        ui_dir = Path(project_root) / "components" / "ui"
        installed = _scan_installed(ui_dir)
        if not installed:
            # Empty dir / missing tree — degrade gracefully but warn.
            logger.debug(
                "ui_component_registry: no components found under %s; "
                "returning unfiltered catalogue",
                ui_dir,
            )

    else:
        installed = set()

    out: list[dict] = []
    for name, comp in sorted(REGISTRY.items()):
        if project_root is not None and installed and name not in installed:
            continue
        if category is not None and comp.category != category:
            continue
        out.append(_serialise(comp))
    return out


def get_component(name: str) -> ShadcnComponent | None:
    """Return the registry entry for ``name`` or None if unknown."""
    return REGISTRY.get(name)


def list_component_names() -> list[str]:
    """Return all registered component names, sorted."""
    return sorted(REGISTRY.keys())


def get_components_by_category(category: str) -> list[ShadcnComponent]:
    """Return all registry entries with the given category."""
    if category not in CATEGORIES:
        raise ValueError(
            f"Unknown category {category!r}; must be one of {CATEGORIES}"
        )
    return sorted(
        (c for c in REGISTRY.values() if c.category == category),
        key=lambda c: c.name,
    )


def find_missing_on_disk(project_root: Path) -> list[str]:
    """Return component stems present on disk but NOT in the registry.

    Useful for CI:  ``assert find_missing_on_disk(root) == []`` catches
    the case where someone adds a new shadcn component file without
    updating the registry.
    """
    installed = _scan_installed(Path(project_root) / "components" / "ui")
    return sorted(installed - set(REGISTRY.keys()))


def render_agent_context_block(
    project_root: Path | None = None,
    categories: Iterable[str] | None = None,
) -> str:
    """Render a compact markdown block for LLM context injection.

    The output is deterministic (sorted by category → name) so cache
    keys stay stable.  Keep this short — the UI Designer skill is
    already ~250 lines of prompt, and the registry injection needs to
    fit the token budget.
    """
    cats = tuple(categories) if categories else CATEGORIES
    lines: list[str] = [
        f"# shadcn/ui component registry (v{REGISTRY_SCHEMA_VERSION})",
        "",
        "Use these components instead of raw HTML.  Import from "
        "`@/components/ui/<name>`.",
        "",
    ]

    components = get_available_components(project_root=project_root)
    by_cat: dict[str, list[dict]] = {c: [] for c in cats}
    for c in components:
        if c["category"] in by_cat:
            by_cat[c["category"]].append(c)

    for cat in cats:
        entries = by_cat[cat]
        if not entries:
            continue
        lines.append(f"## {cat}")
        for c in entries:
            exports = ", ".join(c["exports"])
            lines.append(f"- **{c['name']}** — {c['summary']}")
            lines.append(f"  - exports: {exports}")
            if c.get("variants"):
                vparts = [
                    f"{v['name']}={'|'.join(v['values'])}" for v in c["variants"]
                ]
                lines.append(f"  - variants: {', '.join(vparts)}")
            if c.get("aria_pattern"):
                lines.append(f"  - aria: {c['aria_pattern']}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _serialise(comp: ShadcnComponent) -> dict:
    """Convert a component dataclass to a JSON-safe dict."""
    d = asdict(comp)
    d["import_path"] = comp.import_path
    # Normalise tuples → lists so the result is json.dumps-able.
    d["exports"] = list(comp.exports)
    d["notes"] = list(comp.notes)
    d["props"] = [asdict(p) for p in comp.props]
    d["variants"] = [
        {
            "name": v.name,
            "values": list(v.values),
            "default": v.default,
        }
        for v in comp.variants
    ]
    return d


__all__ = [
    "CATEGORIES",
    "REGISTRY",
    "REGISTRY_SCHEMA_VERSION",
    "ComponentProp",
    "ComponentVariant",
    "ShadcnComponent",
    "find_missing_on_disk",
    "get_available_components",
    "get_component",
    "get_components_by_category",
    "list_component_names",
    "render_agent_context_block",
]


if __name__ == "__main__":  # pragma: no cover
    print(json.dumps(get_available_components(), indent=2))
