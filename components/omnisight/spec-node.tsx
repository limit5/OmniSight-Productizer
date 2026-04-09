"use client"

import { useState } from "react"
import { ChevronDown, ChevronRight, Edit3, Check, X, Plus, Minus, Trash2, FolderPlus, FilePlus } from "lucide-react"

interface SpecValue {
  key: string
  value: string | number | boolean | SpecValue[]
  type: "hardware" | "software" | "config" | "default"
  editable?: boolean
  deletable?: boolean
  options?: string[]
  step?: number // For numeric increment/decrement
  min?: number
  max?: number
}

// Predefined spec templates for common additions
const SPEC_TEMPLATES: Record<string, { label: string; type: SpecValue["type"]; items: Omit<SpecValue, "type">[] }> = {
  gpio: {
    label: "GPIO",
    type: "hardware",
    items: [
      { key: "gpio_count", value: 4, editable: true, deletable: true, step: 1, min: 1, max: 40 },
      { key: "gpio_voltage", value: "3.3V", editable: true, deletable: true, options: ["1.8V", "3.3V", "5V"] },
      { key: "gpio_pullup", value: true, editable: true, deletable: true },
    ]
  },
  audio: {
    label: "Audio",
    type: "hardware",
    items: [
      { key: "microphone", value: "PDM_MEMS", editable: true, deletable: true, options: ["PDM_MEMS", "I2S_MEMS", "Analog", "None"] },
      { key: "mic_channels", value: 1, editable: true, deletable: true, step: 1, min: 1, max: 8 },
      { key: "speaker", value: "I2S_Amp", editable: true, deletable: true, options: ["I2S_Amp", "PWM", "Line_Out", "None"] },
      { key: "sample_rate", value: 48000, editable: true, deletable: true, step: 8000, min: 8000, max: 96000 },
    ]
  },
  display: {
    label: "Display",
    type: "hardware",
    items: [
      { key: "display_type", value: "LCD_IPS", editable: true, deletable: true, options: ["LCD_IPS", "LCD_TN", "OLED", "E-Ink", "None"] },
      { key: "display_size", value: "2.4inch", editable: true, deletable: true, options: ["1.3inch", "2.4inch", "3.5inch", "5inch", "7inch"] },
      { key: "display_resolution", value: "320x240", editable: true, deletable: true, options: ["128x64", "240x240", "320x240", "480x320", "800x480"] },
      { key: "touch", value: false, editable: true, deletable: true },
    ]
  },
  led: {
    label: "LED Lighting",
    type: "hardware",
    items: [
      { key: "led_type", value: "IR_850nm", editable: true, deletable: true, options: ["IR_850nm", "IR_940nm", "White_LED", "RGB", "None"] },
      { key: "led_count", value: 4, editable: true, deletable: true, step: 1, min: 1, max: 24 },
      { key: "led_power", value: "1W", editable: true, deletable: true, options: ["0.5W", "1W", "3W", "5W"] },
      { key: "led_control", value: "PWM", editable: true, deletable: true, options: ["PWM", "GPIO", "I2C"] },
    ]
  },
  onvif: {
    label: "ONVIF",
    type: "config",
    items: [
      { key: "onvif_version", value: "2.6", editable: true, deletable: true, options: ["2.4", "2.5", "2.6", "21.06"] },
      { key: "profile_s", value: true, editable: true, deletable: true },
      { key: "profile_t", value: true, editable: true, deletable: true },
      { key: "ptz_support", value: false, editable: true, deletable: true },
      { key: "analytics", value: true, editable: true, deletable: true },
    ]
  },
  uvc: {
    label: "UVC Extension",
    type: "config",
    items: [
      { key: "uvc_version", value: "1.5", editable: true, deletable: true, options: ["1.1", "1.5"] },
      { key: "xu_unit_id", value: 6, editable: true, deletable: true, step: 1, min: 1, max: 255 },
      { key: "xu_control_selector", value: "0x01", editable: true, deletable: true },
      { key: "xu_data_length", value: 32, editable: true, deletable: true, step: 8, min: 8, max: 256 },
      { key: "xu_get_cur", value: true, editable: true, deletable: true },
      { key: "xu_set_cur", value: true, editable: true, deletable: true },
    ]
  },
  protocol: {
    label: "Protocols",
    type: "config",
    items: [
      { key: "http_port", value: 80, editable: true, deletable: true, step: 1, min: 1, max: 65535 },
      { key: "https_port", value: 443, editable: true, deletable: true, step: 1, min: 1, max: 65535 },
      { key: "rtsp_enabled", value: true, editable: true, deletable: true },
      { key: "mqtt_enabled", value: false, editable: true, deletable: true },
      { key: "websocket", value: true, editable: true, deletable: true },
    ]
  },
  storage: {
    label: "Storage",
    type: "hardware",
    items: [
      { key: "sd_card", value: true, editable: true, deletable: true },
      { key: "sd_max_size", value: "256GB", editable: true, deletable: true, options: ["32GB", "64GB", "128GB", "256GB", "512GB"] },
      { key: "emmc", value: false, editable: true, deletable: true },
      { key: "emmc_size", value: "8GB", editable: true, deletable: true, options: ["4GB", "8GB", "16GB", "32GB"] },
    ]
  },
  power: {
    label: "Power",
    type: "hardware",
    items: [
      { key: "input_voltage", value: "12V_DC", editable: true, deletable: true, options: ["5V_DC", "12V_DC", "24V_DC", "PoE", "PoE+"] },
      { key: "power_consumption", value: "5W", editable: true, deletable: true, options: ["2W", "5W", "10W", "15W", "25W"] },
      { key: "poe_class", value: "Class_3", editable: true, deletable: true, options: ["Class_1", "Class_2", "Class_3", "Class_4", "802.3bt"] },
    ]
  },
  custom: {
    label: "Custom Section",
    type: "default",
    items: [
      { key: "custom_key", value: "custom_value", editable: true, deletable: true },
    ]
  }
}

interface SpecNodeProps {
  spec?: SpecValue[]
  onSpecChange?: (path: string[], newValue: string | number | boolean) => void
  onAddSection?: (sectionKey: string, items: SpecValue[]) => void
  onRemoveSection?: (sectionKey: string) => void
  onAddItem?: (sectionKey: string, item: SpecValue) => void
  onRemoveItem?: (sectionKey: string, itemKey: string) => void
}

function getTypeColor(type: string): string {
  switch (type) {
    case "hardware": return "var(--hardware-orange)"
    case "software": return "var(--artifact-purple)"
    case "config": return "var(--neural-blue)"
    default: return "var(--foreground)"
  }
}

function getTypeBgClass(type: string): string {
  switch (type) {
    case "hardware": return "bg-[var(--hardware-orange-dim)]"
    case "software": return "bg-[var(--artifact-purple-dim)]"
    case "config": return "bg-[var(--neural-blue-dim)]"
    default: return ""
  }
}

interface SpecLineProps {
  item: SpecValue
  depth: number
  path: string[]
  onValueChange?: (path: string[], newValue: string | number | boolean) => void
  onRemoveItem?: (sectionKey: string, itemKey: string) => void
  onAddItem?: (sectionKey: string, item: SpecValue) => void
  isSection?: boolean
  onRemoveSection?: (sectionKey: string) => void
}

function SpecLine({ item, depth, path, onValueChange, onRemoveItem, onAddItem, isSection, onRemoveSection }: SpecLineProps) {
  const [expanded, setExpanded] = useState(true)
  const [editing, setEditing] = useState(false)
  const [editValue, setEditValue] = useState(String(item.value))
  const [showOptions, setShowOptions] = useState(false)
  const [showAddMenu, setShowAddMenu] = useState(false)
  const [newItemKey, setNewItemKey] = useState("")
  const [newItemValue, setNewItemValue] = useState("")
  const [newItemType, setNewItemType] = useState<"string" | "number" | "boolean">("string")
  const isNested = Array.isArray(item.value)
  const isNumeric = typeof item.value === "number"
  const isBoolean = typeof item.value === "boolean"
  const hasStepControls = isNumeric && item.editable && item.step !== undefined

  const handleSave = () => {
    if (onValueChange) {
      const newVal = typeof item.value === "number" ? Number(editValue) : editValue
      onValueChange([...path, item.key], newVal)
    }
    setEditing(false)
  }

  const handleOptionSelect = (option: string) => {
    setEditValue(option)
    if (onValueChange) {
      onValueChange([...path, item.key], option)
    }
    setShowOptions(false)
  }

  const handleIncrement = () => {
    if (!isNumeric || !onValueChange) return
    const step = item.step || 1
    const max = item.max ?? Infinity
    const newVal = Math.min((item.value as number) + step, max)
    onValueChange([...path, item.key], newVal)
  }

  const handleDecrement = () => {
    if (!isNumeric || !onValueChange) return
    const step = item.step || 1
    const min = item.min ?? -Infinity
    const newVal = Math.max((item.value as number) - step, min)
    onValueChange([...path, item.key], newVal)
  }

  const handleToggleBoolean = () => {
    if (!isBoolean || !onValueChange) return
    onValueChange([...path, item.key], !item.value)
  }

  const handleAddNewItem = () => {
    if (!newItemKey.trim() || !onAddItem) return
    let value: string | number | boolean = newItemValue
    if (newItemType === "number") value = Number(newItemValue) || 0
    if (newItemType === "boolean") value = newItemValue === "true"
    
    const newItem: SpecValue = {
      key: newItemKey.toLowerCase().replace(/\s+/g, "_"),
      value,
      type: item.type,
      editable: true,
      deletable: true
    }
    onAddItem(item.key, newItem)
    setNewItemKey("")
    setNewItemValue("")
    setShowAddMenu(false)
  }

  const handleRemoveThisItem = () => {
    if (!onRemoveItem || path.length === 0) return
    const sectionKey = path[path.length - 1]
    onRemoveItem(sectionKey, item.key)
  }

  return (
    <div className="select-none">
      <div 
        className={`flex items-center justify-between gap-3 py-0.5 px-1.5 rounded transition-all duration-200 hover:bg-[var(--holo-glass)] group ${editing ? getTypeBgClass(item.type) + " pulse-" + (item.type === "hardware" ? "orange" : item.type === "software" ? "purple" : "blue") : ""}`}
        style={{ paddingLeft: `${depth * 12 + 4}px` }}
      >
        {/* Left: Expand/Collapse + Key */}
        <div className="flex items-center gap-1 shrink-0">
          {isNested ? (
            <button 
              onClick={() => setExpanded(!expanded)}
              className="w-3.5 h-3.5 flex items-center justify-center text-[var(--muted-foreground)] hover:text-[var(--neural-blue)] transition-colors shrink-0"
            >
              {expanded ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
            </button>
          ) : (
            <span className="w-3.5 shrink-0" />
          )}
          
          <span className="code-key font-mono text-xs whitespace-nowrap" style={{ color: getTypeColor(item.type) }}>
            {item.key}
          </span>
        </div>
        
        {/* Right: Value + Actions */}
        <div className="flex items-center gap-1.5 shrink-0">
          {!isNested && (
            <>
              {editing ? (
                <div className="flex items-center gap-1.5 relative">
                  {item.options ? (
                    <div className="relative">
                      <button
                        onClick={() => setShowOptions(!showOptions)}
                        className="font-mono text-xs px-1.5 py-0.5 bg-[var(--secondary)] border border-[var(--border)] rounded text-[var(--foreground)] hover:border-[var(--neural-blue)] transition-colors"
                      >
                        {editValue}
                        <ChevronDown size={10} className="inline ml-1" />
                      </button>
                      {showOptions && (
                        <div className="absolute top-full right-0 mt-1 z-50 holo-glass-simple rounded py-1 min-w-[140px] max-h-48 overflow-auto">
                          {item.options.map(opt => (
                            <button
                              key={opt}
                              onClick={() => handleOptionSelect(opt)}
                              className="block w-full text-left px-2 py-1 font-mono text-xs text-[var(--foreground)] hover:bg-[var(--neural-blue-dim)] hover:text-[var(--neural-blue)] transition-colors"
                            >
                              {opt}
                            </button>
                          ))}
                        </div>
                      )}
                    </div>
                  ) : (
                    <input
                      type={typeof item.value === "number" ? "number" : "text"}
                      value={editValue}
                      onChange={e => setEditValue(e.target.value)}
                      className="font-mono text-xs px-1.5 py-0.5 bg-[var(--secondary)] border border-[var(--neural-blue)] rounded text-[var(--foreground)] w-28 focus:outline-none focus:ring-1 focus:ring-[var(--neural-blue)]"
                      autoFocus
                    />
                  )}
                  <button onClick={handleSave} className="text-[var(--validation-emerald)] hover:text-[var(--validation-emerald)]">
                    <Check size={12} />
                  </button>
                  <button onClick={() => { setEditing(false); setEditValue(String(item.value)); setShowOptions(false); }} className="text-[var(--critical-red)]">
                    <X size={12} />
                  </button>
                </div>
              ) : (
                <div className="flex items-center gap-1">
                  {/* Decrement button for numeric values */}
                  {hasStepControls && (
                    <button 
                      onClick={handleDecrement}
                      disabled={item.min !== undefined && (item.value as number) <= item.min}
                      className="opacity-0 group-hover:opacity-100 w-4 h-4 flex items-center justify-center rounded bg-[var(--secondary)] text-[var(--muted-foreground)] hover:text-[var(--critical-red)] hover:bg-[var(--critical-red)]/20 disabled:opacity-30 disabled:cursor-not-allowed transition-all"
                      title={`Decrease by ${item.step}`}
                    >
                      <Minus size={9} />
                    </button>
                  )}
                  
                  {/* Boolean toggle */}
                  {isBoolean && item.editable ? (
                    <button
                      onClick={handleToggleBoolean}
                      className={`font-mono text-xs px-1.5 py-0.5 rounded transition-colors ${
                        item.value 
                          ? "bg-[var(--validation-emerald)]/20 text-[var(--validation-emerald)]" 
                          : "bg-[var(--critical-red)]/20 text-[var(--critical-red)]"
                      }`}
                    >
                      {item.value ? "true" : "false"}
                    </button>
                  ) : (
                    <span 
                      className={`font-mono text-xs ${item.editable && !hasStepControls && !isBoolean ? "cursor-pointer hover:underline" : ""} ${hasStepControls ? "min-w-[50px] text-center" : ""}`}
                      style={{ color: typeof item.value === "number" ? "var(--hardware-orange)" : typeof item.value === "boolean" ? "var(--validation-emerald)" : "var(--validation-emerald)" }}
                      onClick={() => item.editable && !hasStepControls && !isBoolean && setEditing(true)}
                    >
                      {typeof item.value === "boolean" ? (item.value ? "true" : "false") : String(item.value)}
                    </span>
                  )}
                  
                  {/* Increment button for numeric values */}
                  {hasStepControls && (
                    <button 
                      onClick={handleIncrement}
                      disabled={item.max !== undefined && (item.value as number) >= item.max}
                      className="opacity-0 group-hover:opacity-100 w-4 h-4 flex items-center justify-center rounded bg-[var(--secondary)] text-[var(--muted-foreground)] hover:text-[var(--validation-emerald)] hover:bg-[var(--validation-emerald)]/20 disabled:opacity-30 disabled:cursor-not-allowed transition-all"
                      title={`Increase by ${item.step}`}
                    >
                      <Plus size={9} />
                    </button>
                  )}
                  {/* Edit button */}
                  {item.editable && !hasStepControls && !isBoolean && (
                    <button 
                      onClick={() => setEditing(true)}
                      className="opacity-0 group-hover:opacity-100 text-[var(--muted-foreground)] hover:text-[var(--neural-blue)] transition-all"
                    >
                      <Edit3 size={10} />
                    </button>
                  )}
                  
                  {/* Delete button for deletable items */}
                  {item.deletable && onRemoveItem && (
                    <button 
                      onClick={handleRemoveThisItem}
                      className="opacity-0 group-hover:opacity-100 text-[var(--muted-foreground)] hover:text-[var(--critical-red)] transition-all"
                      title="Remove this item"
                    >
                      <Trash2 size={10} />
                    </button>
                  )}
                </div>
              )}
            </>
          )}
          
          {/* Section-level controls */}
          {isSection && isNested && (
            <div className="flex items-center gap-0.5 opacity-0 group-hover:opacity-100 transition-all">
              <button 
                onClick={() => setShowAddMenu(!showAddMenu)}
                className="w-4 h-4 flex items-center justify-center rounded bg-[var(--secondary)] text-[var(--muted-foreground)] hover:text-[var(--validation-emerald)] hover:bg-[var(--validation-emerald)]/20 transition-all"
                title="Add item to this section"
              >
                <FilePlus size={10} />
              </button>
              {onRemoveSection && (
                <button 
                  onClick={() => onRemoveSection(item.key)}
                  className="w-4 h-4 flex items-center justify-center rounded bg-[var(--secondary)] text-[var(--muted-foreground)] hover:text-[var(--critical-red)] hover:bg-[var(--critical-red)]/20 transition-all"
                  title="Remove this section"
                >
                  <Trash2 size={10} />
                </button>
              )}
            </div>
          )}
        </div>
      </div>
      
      {/* Add item form for sections */}
      {isSection && showAddMenu && (
        <div 
          className="holo-glass-simple rounded p-2 mx-1.5 my-1 space-y-1.5"
          style={{ marginLeft: `${depth * 12 + 16}px` }}
        >
          <div className="flex items-center gap-2">
            <input
              type="text"
              value={newItemKey}
              onChange={e => setNewItemKey(e.target.value)}
              placeholder="key_name"
              className="flex-1 font-mono text-xs px-2 py-1 bg-[var(--secondary)] border border-[var(--border)] rounded text-[var(--foreground)] focus:outline-none focus:border-[var(--neural-blue)]"
            />
            <select
              value={newItemType}
              onChange={e => setNewItemType(e.target.value as "string" | "number" | "boolean")}
              className="font-mono text-xs px-2 py-1 bg-[var(--secondary)] border border-[var(--border)] rounded text-[var(--foreground)]"
            >
              <option value="string">string</option>
              <option value="number">number</option>
              <option value="boolean">boolean</option>
            </select>
          </div>
          <div className="flex items-center gap-2">
            {newItemType === "boolean" ? (
              <select
                value={newItemValue}
                onChange={e => setNewItemValue(e.target.value)}
                className="flex-1 font-mono text-xs px-2 py-1 bg-[var(--secondary)] border border-[var(--border)] rounded text-[var(--foreground)]"
              >
                <option value="true">true</option>
                <option value="false">false</option>
              </select>
            ) : (
              <input
                type={newItemType === "number" ? "number" : "text"}
                value={newItemValue}
                onChange={e => setNewItemValue(e.target.value)}
                placeholder="value"
                className="flex-1 font-mono text-xs px-2 py-1 bg-[var(--secondary)] border border-[var(--border)] rounded text-[var(--foreground)] focus:outline-none focus:border-[var(--neural-blue)]"
              />
            )}
            <button 
              onClick={handleAddNewItem}
              disabled={!newItemKey.trim()}
              className="px-2 py-1 rounded text-xs font-mono bg-[var(--validation-emerald)]/20 text-[var(--validation-emerald)] hover:bg-[var(--validation-emerald)]/30 disabled:opacity-50 disabled:cursor-not-allowed"
            >
              Add
            </button>
            <button 
              onClick={() => { setShowAddMenu(false); setNewItemKey(""); setNewItemValue(""); }}
              className="px-2 py-1 rounded text-xs font-mono text-[var(--muted-foreground)] hover:text-[var(--critical-red)]"
            >
              Cancel
            </button>
          </div>
        </div>
      )}
      
      {isNested && expanded && (
        <div>
          {(item.value as SpecValue[]).map((child, idx) => (
            <SpecLine 
              key={`${item.key}-${child.key}-${idx}`}
              item={child}
              depth={depth + 1}
              path={[...path, item.key]}
              onValueChange={onValueChange}
              onRemoveItem={onRemoveItem}
              onAddItem={onAddItem}
            />
          ))}
        </div>
      )}
    </div>
  )
}

export function SpecNode({ 
  spec: externalSpec, 
  onSpecChange,
  onAddSection,
  onRemoveSection,
  onAddItem,
  onRemoveItem
}: SpecNodeProps) {
  const [internalSpec, setInternalSpec] = useState<SpecValue[]>([])
  const [showAddSectionMenu, setShowAddSectionMenu] = useState(false)
  
  // Use external spec if provided, otherwise use internal state
  const spec = externalSpec ?? internalSpec
  
  // Internal handlers when no external handlers provided
  const handleAddSection = (templateKey: string) => {
    const template = SPEC_TEMPLATES[templateKey]
    if (!template) return
    
    const newSection: SpecValue = {
      key: templateKey,
      type: template.type,
      value: template.items.map(item => ({ ...item, type: template.type })) as SpecValue[]
    }
    
    if (onAddSection) {
      onAddSection(templateKey, newSection.value as SpecValue[])
    } else {
      setInternalSpec(prev => [...prev, newSection])
    }
    setShowAddSectionMenu(false)
  }
  
  const handleRemoveSection = (sectionKey: string) => {
    if (onRemoveSection) {
      onRemoveSection(sectionKey)
    } else {
      setInternalSpec(prev => prev.filter(s => s.key !== sectionKey))
    }
  }
  
  const handleAddItem = (sectionKey: string, item: SpecValue) => {
    if (onAddItem) {
      onAddItem(sectionKey, item)
    } else {
      setInternalSpec(prev => prev.map(section => {
        if (section.key === sectionKey && Array.isArray(section.value)) {
          return { ...section, value: [...section.value, item] }
        }
        return section
      }))
    }
  }
  
  const handleRemoveItem = (sectionKey: string, itemKey: string) => {
    if (onRemoveItem) {
      onRemoveItem(sectionKey, itemKey)
    } else {
      setInternalSpec(prev => prev.map(section => {
        if (section.key === sectionKey && Array.isArray(section.value)) {
          return { ...section, value: section.value.filter(i => i.key !== itemKey) }
        }
        return section
      }))
    }
  }
  
  const handleValueChange = (path: string[], newValue: string | number | boolean) => {
    if (onSpecChange) {
      onSpecChange(path, newValue)
    } else {
      // Internal state update
      setInternalSpec(prev => {
        const updateNested = (items: SpecValue[], pathIndex: number): SpecValue[] => {
          if (pathIndex >= path.length) return items
          
          return items.map(item => {
            if (item.key === path[pathIndex]) {
              if (pathIndex === path.length - 1) {
                return { ...item, value: newValue }
              }
              if (Array.isArray(item.value)) {
                return { ...item, value: updateNested(item.value, pathIndex + 1) }
              }
            }
            return item
          })
        }
        return updateNested(prev, 0)
      })
    }
  }
  
  // Get available templates (excluding already added sections)
  const existingSections = spec.map(s => s.key)
  const availableTemplates = Object.entries(SPEC_TEMPLATES).filter(
    ([key]) => !existingSections.includes(key)
  )
  
  return (
    <div className="holo-glass h-full flex flex-col corner-brackets-full">
      {/* Header */}
      <div className="px-4 py-3 border-b border-[var(--border)] relative circuit-pattern">
        <div className="flex items-center justify-between relative z-10">
          <div className="flex items-center gap-2">
            <div className="w-2 h-2 rounded-full bg-[var(--neural-blue)] pulse-blue neon-border" />
            <h2 className="font-sans text-sm font-semibold tracking-fui text-[var(--neural-blue)]">
              SPEC MATRIX
            </h2>
          </div>
          <button
            onClick={() => setShowAddSectionMenu(!showAddSectionMenu)}
            className="p-1.5 rounded bg-[var(--neural-blue)]/20 hover:bg-[var(--neural-blue)]/40 text-[var(--neural-blue)] transition-colors"
            title="Add specification section"
          >
            <FolderPlus size={14} />
          </button>
        </div>
        <p className="font-mono text-xs text-[var(--muted-foreground)] mt-1">
          SOURCE OF TRUTH NODE
        </p>
      </div>
      
      {/* Add Section Menu */}
      {showAddSectionMenu && (
        <div className="px-2 py-2 border-b border-[var(--border)] bg-[var(--secondary)]/30">
          <p className="font-mono text-xs text-[var(--muted-foreground)] mb-2 px-2">ADD SECTION:</p>
          <div className="flex flex-wrap gap-1">
            {availableTemplates.map(([key, template]) => (
              <button
                key={key}
                onClick={() => handleAddSection(key)}
                className="px-2 py-1 rounded text-xs font-mono transition-colors"
                style={{ 
                  backgroundColor: `color-mix(in srgb, ${getTypeColor(template.type)} 20%, transparent)`,
                  color: getTypeColor(template.type)
                }}
              >
                {template.label}
              </button>
            ))}
          </div>
          {availableTemplates.length === 0 && (
            <p className="font-mono text-xs text-[var(--muted-foreground)] px-2">All sections added</p>
          )}
        </div>
      )}
      
      {/* Code Wall */}
      <div className="flex-1 overflow-auto p-2">
        <div className="font-mono text-sm">
          {spec.map((item, idx) => (
            <SpecLine 
              key={`${item.key}-${idx}`}
              item={item}
              depth={0}
              path={[]}
              onValueChange={handleValueChange}
              onRemoveItem={handleRemoveItem}
              onAddItem={handleAddItem}
              isSection={true}
              onRemoveSection={handleRemoveSection}
            />
          ))}
        </div>
      </div>
      
      {/* Footer */}
      <div className="px-4 py-2 border-t border-[var(--border)] flex items-center justify-between">
        <span className="font-mono text-xs text-[var(--muted-foreground)]">
          {spec.length} sections
        </span>
        <div className="flex items-center gap-2">
          <span className="w-2 h-2 rounded-full bg-[var(--validation-emerald)] status-dot-active" />
          <span className="font-mono text-xs text-[var(--validation-emerald)]">SYNCED</span>
        </div>
      </div>
    </div>
  )
}
