'use client';
import { useState } from 'react';
import InventoryUpload from '@/components/InventoryUpload';
import { FileSpreadsheet, BrainCircuit, RefreshCcw, CheckCircle2, Database } from 'lucide-react';

export default function InventoryPage() {
  const [excelData, setExcelData] = useState<any[]>([]);
  const [excelHeaders, setExcelHeaders] = useState<string[]>([]);
  const [mapping, setMapping] = useState<any>(null);
  const [showMapping, setShowMapping] = useState(false);
  const [isMapping, setIsMapping] = useState(false);
  const [isPushing, setIsPushing] = useState(false);

    const handlePushToDB = async () => {
        if (!mapping || excelData.length === 0) return;
        
        setIsPushing(true);
        try {
            const response = await fetch('/api/inventory/upsert', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                data: excelData,
                mapping: mapping, // API handle karega property matching
            }),
            });

            const result = await response.json();
            if (result.success) {
            alert(`🎉 100% Accurate! ${result.count} units pushed to database.`);
            setShowMapping(false);
            } else {
            alert("⚠️ Error: " + result.error);
            }
        } catch (err) {
            alert("System Error: Could not connect to DB");
        } finally {
            setIsPushing(false);
        }
    };

  const handleDataExtracted = async (headers: string[], data: any[]) => {
    setExcelHeaders(headers);
    setExcelData(data);
    setShowMapping(true);
    setIsMapping(true);

    try {
      const response = await fetch('/api/inventory/map', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ headers }),
      });
      const result = await response.json();
      if (result.success) setMapping(result.mapping);
    } catch (error) {
      console.error("Mapping Error");
    } finally {
      setIsMapping(false);
    }
  };

  return (
    <div className="h-[calc(100vh-64px)] w-full max-w-7xl mx-auto p-4 flex flex-col gap-4 overflow-hidden">
      <header className="flex justify-between items-center">
        <div>
          <h1 className="text-2xl font-bold text-foreground">Inventory Manager</h1>
          <p className="text-sm text-muted-foreground">Verify AI Mapping & Data Preview</p>
        </div>
        {showMapping && (
          <button onClick={() => {setShowMapping(false); setMapping(null);}} className="flex items-center gap-2 text-xs font-medium px-4 py-2 bg-accent/10 text-accent rounded-lg cursor-pointer">
            <RefreshCcw size={14} /> Re-upload File
          </button>
        )}
      </header>

      <main className="flex-1 min-h-0 bg-background border border-border rounded-2xl overflow-hidden shadow-sm flex flex-col">
        {!showMapping ? (
          <InventoryUpload onDataExtracted={handleDataExtracted} />
        ) : (
          <div className="flex flex-col h-full">
            {/* Table Header Section */}
            <div className="p-4 border-b border-border bg-accent/2 flex justify-between items-center">
              <div className="flex items-center gap-2 text-sm font-semibold">
                <Database size={16} className="text-accent" />
                Preview: {excelData.length} Units Found
              </div>
                <button 
                    onClick={handlePushToDB}
                    disabled={isMapping || isPushing}
                    className="px-6 py-2 bg-foreground text-background rounded-lg font-bold text-sm hover:opacity-90 disabled:opacity-50 cursor-pointer flex items-center gap-2"
                    >
                    {isPushing ? (
                        <>Processing...</>
                    ) : isMapping ? (
                        "AI Mapping..."
                    ) : (
                        <>Push to Database</>
                    )}
                </button>
            </div>

            {/* 🔥 THE REAL PREVIEW TABLE */}
            <div className="flex-1 overflow-auto">
              <table className="w-full text-left border-collapse">
                <thead className="sticky top-0 bg-background border-b border-border z-10">
                    <tr>
                        <th className="p-3 text-[10px] font-bold uppercase text-muted-foreground bg-muted/30 text-left">Unit Name</th>
                        <th className="p-3 text-[10px] font-bold uppercase text-muted-foreground bg-muted/30 text-left">Price</th>
                        <th className="p-3 text-[10px] font-bold uppercase text-muted-foreground bg-muted/30 text-left">Area</th>
                        <th className="p-3 text-[10px] font-bold uppercase text-muted-foreground bg-muted/30 text-left">Config</th>
                        <th className="p-3 text-[10px] font-bold uppercase text-muted-foreground bg-muted/30 text-left">Status</th>
                        <th className="p-3 text-[10px] font-bold uppercase text-muted-foreground bg-muted/30 text-left">Metadata (Other)</th>
                    </tr>
                </thead>
                <tbody className="divide-y divide-border">
                    {excelData.slice(0, 15).map((row, idx) => {
                        const getVal = (sysCol: string) => {
                        const excelCol = Object.keys(mapping || {}).find(key => mapping[key] === sysCol);
                        return excelCol ? row[excelCol] : '-';
                        };

                        const metaKeys = Object.keys(mapping || {}).filter(key => mapping[key] === 'metadata');
                        const metadataObj = metaKeys.reduce((acc, key) => ({ ...acc, [key]: row[key] }), {});

                        return (
                        <tr key={idx} className="hover:bg-accent/2">
                            <td className="p-3 text-sm font-medium">{getVal('unit_name')}</td>
                            <td className="p-3 text-sm text-green-600 font-semibold">{getVal('price')}</td>
                            <td className="p-3 text-sm">{getVal('carpet_area')}</td>
                            <td className="p-3 text-sm">{getVal('configuration')}</td>
                            <td className="p-3 text-sm">
                            <span className="px-2 py-0.5 rounded-full bg-blue-500/10 text-blue-600 text-[10px] font-bold">
                                {getVal('status')}
                            </span>
                            </td>
                            <td className="p-3">
                            {Object.keys(metadataObj).length > 0 ? (
                                <span className="text-[10px] bg-purple-500/10 text-purple-600 px-2 py-1 rounded border border-purple-200 block truncate max-w-37.5">
                                {JSON.stringify(metadataObj)}
                                </span>
                            ) : (
                                <span className="text-gray-300 text-[10px]">None</span>
                            )}
                            </td>
                        </tr>
                        );
                    })}
                </tbody>
              </table>
              {excelData.length > 15 && (
                <p className="p-4 text-center text-xs text-muted-foreground italic">+ {excelData.length - 15} more units...</p>
              )}
            </div>
          </div>
        )}
      </main>
    </div>
  );
}