"use client"

import { useEffect, useState } from "react"
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from "@/components/ui/dialog"
import { GitBranch, FileUp, PenLine, Workflow } from "lucide-react"
import type { PanelId } from "@/components/omnisight/mobile-nav"

const LS_LAST_SPEC = "omnisight:intent:last_spec"
const LS_WIZARD_SEEN = "omnisight:wizard:seen"

interface WizardChoice {
  id: string
  label: string
  description: string
  icon: React.ReactNode
  panel: PanelId
}

const CHOICES: WizardChoice[] = [
  {
    id: "github",
    label: "GitHub Repo",
    description: "Import an existing repository and auto-detect project structure",
    icon: <GitBranch className="size-6" />,
    panel: "spec",
  },
  {
    id: "upload",
    label: "Upload Docs",
    description: "Upload specification documents, datasheets, or requirements",
    icon: <FileUp className="size-6" />,
    panel: "spec",
  },
  {
    id: "prose",
    label: "Describe in Prose",
    description: "Write a free-form description of what you want to build",
    icon: <PenLine className="size-6" />,
    panel: "spec",
  },
  {
    id: "blank",
    label: "Blank DAG",
    description: "Start with an empty DAG and define tasks manually",
    icon: <Workflow className="size-6" />,
    panel: "dag",
  },
]

function navigateToPanel(panel: PanelId) {
  window.dispatchEvent(
    new CustomEvent("omnisight:navigate", { detail: { panel } }),
  )
}

export function NewProjectWizard() {
  const [open, setOpen] = useState(false)

  useEffect(() => {
    if (typeof window === "undefined") return
    const hasSpec = !!localStorage.getItem(LS_LAST_SPEC)
    const wizardSeen = !!localStorage.getItem(LS_WIZARD_SEEN)
    if (!hasSpec && !wizardSeen) {
      setOpen(true) // eslint-disable-line react-hooks/set-state-in-effect -- mount-time check of localStorage
    }
  }, [])

  function handleChoice(choice: WizardChoice) {
    localStorage.setItem(LS_WIZARD_SEEN, "1")
    setOpen(false)
    navigateToPanel(choice.panel)
  }

  function handleDismiss(openState: boolean) {
    if (!openState) {
      localStorage.setItem(LS_WIZARD_SEEN, "1")
      setOpen(false)
    }
  }

  return (
    <Dialog open={open} onOpenChange={handleDismiss}>
      <DialogContent
        className="sm:max-w-lg"
        data-testid="new-project-wizard"
      >
        <DialogHeader>
          <DialogTitle>Start a New Project</DialogTitle>
          <DialogDescription>
            Choose how you want to begin. You can always change your approach later.
          </DialogDescription>
        </DialogHeader>
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 pt-2">
          {CHOICES.map((choice) => (
            <button
              key={choice.id}
              data-testid={`wizard-choice-${choice.id}`}
              onClick={() => handleChoice(choice)}
              className="flex flex-col items-start gap-2 rounded-lg border border-border p-4 text-left transition-colors hover:bg-accent hover:text-accent-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              <div className="text-muted-foreground">{choice.icon}</div>
              <div className="font-medium text-sm">{choice.label}</div>
              <div className="text-xs text-muted-foreground leading-snug">
                {choice.description}
              </div>
            </button>
          ))}
        </div>
      </DialogContent>
    </Dialog>
  )
}
