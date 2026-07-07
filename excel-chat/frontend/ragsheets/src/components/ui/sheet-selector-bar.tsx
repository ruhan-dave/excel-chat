import { useState, useRef, useEffect } from "react";
import { X, Plus, FileSpreadsheet, Check } from "lucide-react";
import type { SheetInfo } from "@/lib/api";

interface SheetSelectorBarProps {
    selectedSheets: SheetInfo[];
    allSheets: SheetInfo[];
    onAddSheet: (sheetId: string) => void;
    onRemoveSheet: (sheetId: string) => void;
}

export function SheetSelectorBar({
    selectedSheets,
    allSheets,
    onAddSheet,
    onRemoveSheet,
}: SheetSelectorBarProps) {
    const [showPicker, setShowPicker] = useState(false);
    const pickerRef = useRef<HTMLDivElement>(null);

    const availableSheets = allSheets.filter(
        (s) => !selectedSheets.find((sel) => sel.sheet_id === s.sheet_id)
    );

    useEffect(() => {
        const handleClickOutside = (e: MouseEvent) => {
            if (pickerRef.current && !pickerRef.current.contains(e.target as Node)) {
                setShowPicker(false);
            }
        };
        if (showPicker) {
            document.addEventListener("mousedown", handleClickOutside);
        }
        return () => document.removeEventListener("mousedown", handleClickOutside);
    }, [showPicker]);

    return (
        <div className="flex flex-wrap items-center gap-1.5">
            {selectedSheets.map((sheet) => (
                <div
                    key={sheet.sheet_id}
                    className="group flex items-center gap-1.5 rounded-md border border-blue-200 bg-blue-50 px-2 py-1 text-xs text-blue-800"
                >
                    <FileSpreadsheet className="h-3 w-3 text-blue-600" />
                    <span className="max-w-[120px] truncate">{sheet.sheet_name}</span>
                    <button
                        className="text-blue-400 hover:text-blue-600"
                        onClick={() => onRemoveSheet(sheet.sheet_id)}
                    >
                        <X className="h-3 w-3" />
                    </button>
                </div>
            ))}

            {/* Add sheet picker */}
            <div className="relative" ref={pickerRef}>
                <button
                    className="flex items-center gap-1 rounded-md border border-dashed border-slate-300 px-2 py-1 text-xs text-muted-foreground hover:border-primary/40 hover:text-foreground"
                    onClick={() => setShowPicker(!showPicker)}
                >
                    <Plus className="h-3 w-3" />
                    Add sheet
                </button>

                {showPicker && (
                    <div className="absolute top-full left-0 z-50 mt-1 w-64 rounded-md border border-slate-200 bg-white shadow-lg">
                        {availableSheets.length === 0 ? (
                            <div className="px-3 py-2 text-xs text-muted-foreground">
                                All sheets are already selected.
                            </div>
                        ) : (
                            <div className="max-h-48 overflow-y-auto py-1">
                                {availableSheets.map((sheet) => (
                                    <div
                                        key={sheet.sheet_id}
                                        className="flex cursor-pointer items-center gap-2 px-3 py-1.5 hover:bg-slate-50"
                                        onClick={() => {
                                            onAddSheet(sheet.sheet_id);
                                            setShowPicker(false);
                                        }}
                                    >
                                        <FileSpreadsheet className="h-3.5 w-3.5 text-emerald-600" />
                                        <div className="flex-1 overflow-hidden">
                                            <div className="truncate text-xs font-medium">
                                                {sheet.sheet_name}
                                            </div>
                                            <div className="truncate text-[10px] text-muted-foreground">
                                                {sheet.file_name}
                                            </div>
                                        </div>
                                        <Check className="h-3 w-3 text-transparent" />
                                    </div>
                                ))}
                            </div>
                        )}
                    </div>
                )}
            </div>
        </div>
    );
}
