/**
 * Q.7 #301 — ``use409Conflict`` hook.
 *
 * Wraps the Q.7 optimistic-lock contract in the shape React consumers
 * actually need:
 *
 *   const { handle } = use409Conflict()
 *   try {
 *     await patchTask(taskId, newFields, version)
 *   } catch (err) {
 *     const consumed = handle(err, {
 *       onReload: () => void refetch(),
 *       onOverwrite: async () => {
 *         const fresh = await refetch()
 *         await patchTask(taskId, newFields, fresh.version)
 *       },
 *       // onMerge intentionally omitted — no merge strategy for this
 *       // field; the toast hides the 合併 button automatically.
 *     })
 *     if (!consumed) throw err
 *   }
 *
 * The hook itself is stateless — resolution handlers are passed per
 * call so each callsite can wire its own re-fetch / clobber logic.
 * The toast UX (``<Conflict409ToastCenter />``) surfaces the 3 FUI
 * buttons "重載 / 覆蓋 / 合併" with 重載 as the default (Enter / Esc
 * both dismiss-reload); this matches the Q.7 spec ("預設重載，符合
 * 社群平台多數做法").
 */

import { useMemo } from "react"

import {
  handleConflict409,
  type Conflict409ResolutionHandlers,
} from "@/lib/conflict-409-bus"

export interface Use409ConflictReturn {
  /**
   * Attempt to translate a caught error into a 409 conflict toast.
   * Returns ``true`` when the error was a Q.7-shaped 409 and an event
   * was emitted to the bus; the caller should NOT re-throw. Returns
   * ``false`` for every other error shape (including 409s whose body
   * doesn't follow the Q.7 contract — those fall through to the
   * generic ``ApiErrorToastCenter``).
   */
  handle: (err: unknown, resolution: Conflict409ResolutionHandlers) => boolean
}

export function use409Conflict(): Use409ConflictReturn {
  return useMemo(
    () => ({
      handle: (err, resolution) => handleConflict409(err, resolution),
    }),
    [],
  )
}

export default use409Conflict
