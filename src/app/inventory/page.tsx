'use client';
import { useState, useEffect, useCallback } from 'react';
import InventoryUpload from '@/components/InventoryUpload';
import { 
  Database, Trash2, Edit3, 
  Search, Filter, Loader2, AlertCircle
} from 'lucide-react';
import { createClient } from '@supabase/supabase-js';

const supabase = createClient(
  process.env.NEXT_PUBLIC_SUPABASE_URL!,
  process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY!
);

export default function InventoryPage() {
  const [activeTab, setActiveTab] = useState<'view' | 'upload'>('view');
  const [dbInventory, setDbInventory] = useState<any[]>([]);
  const [excelData, setExcelData] = useState<any[]>([]);
  const [mapping, setMapping] = useState<any>(null);
  const [isPushing, setIsPushing] = useState(false);
  const [isLoading, setIsLoading] = useState(true);
  const [searchQuery, setSearchQuery] = useState("");

  const fetchInventory = useCallback(async () => {
    setIsLoading(true);
    try {
      const { data, error } = await supabase
        .from('unit_inventory')
        .select('*, properties (name)')
        .order('created_at', { ascending: false });
      
      if (error) throw error;
      setDbInventory(data || []);
    } catch (err) {
      console.error("Fetch Error:", err);
    } finally {
      setIsLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchInventory();
  }, [fetchInventory]);

  // --- CRUD FUNCTIONS START ---

  // 1. DELETE FUNCTION
  const handleDelete = async (id: string) => {
    if (!confirm("Are you sure you want to delete this unit?")) return;
    
    try {
      const { error } = await supabase.from('unit_inventory').delete().eq('id', id);
      if (error) throw error;
      // UI update bina refresh kiye
      setDbInventory(prev => prev.filter(item => item.id !== id));
    } catch (err: any) {
      alert("Delete failed: " + err.message);
    }
  };

  // 2. TOGGLE STATUS FUNCTION
  const handleToggleStatus = async (id: string, currentStatus: string) => {
    const newStatus = currentStatus === 'Available' ? 'Sold' : 'Available';
    try {
      const { error } = await supabase
        .from('unit_inventory')
        .update({ status: newStatus })
        .eq('id', id);
      
      if (error) throw error;
      // Update local state
      setDbInventory(prev => prev.map(item => 
        item.id === id ? { ...item, status: newStatus } : item
      ));
    } catch (err: any) {
      alert("Status update failed");
    }
  };

  // 3. EDIT PRICE (Simple Implementation)
  const handleEditPrice = async (id: string, currentPrice: string) => {
    const newPrice = prompt("Enter new price (numeric):", currentPrice);
    if (newPrice === null || newPrice === currentPrice) return;

    try {
      const { error } = await supabase
        .from('unit_inventory')
        .update({ price: newPrice })
        .eq('id', id);
      
      if (error) throw error;
      setDbInventory(prev => prev.map(item => 
        item.id === id ? { ...item, price: newPrice } : item
      ));
    } catch (err: any) {
      alert("Price update failed");
    }
  };

  // --- CRUD FUNCTIONS END ---

  const filteredData = dbInventory.filter(item => 
    item.unit_name?.toLowerCase().includes(searchQuery) ||
    item.properties?.name?.toLowerCase().includes(searchQuery) ||
    item.configuration?.toLowerCase().includes(searchQuery)
  );

  const handlePushToDB = async () => {
    console.log("Push button clicked!"); // Check if this prints
    console.log("Mapping state:", mapping); // Should NOT be null
    console.log("Data length:", excelData.length);

    if (!mapping || excelData.length === 0) {
      alert("Mapping is missing. Please wait for AI to map headers.");
      return;
    }
    setIsPushing(true);
    try {
      const response = await fetch('/api/inventory/upsert', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ data: excelData, mapping }),
      });
      const result = await response.json();
      if (result.success) {
        setExcelData([]);
        setMapping(null);
        setActiveTab('view');
        fetchInventory();
      }
    } catch (err) {
      alert("Push Failed");
    } finally {
      setIsPushing(false);
    }
  }

  const handleDataExtracted = async (headers: string[], data: any[]) => {
    setExcelData(data);
    setActiveTab('upload');
    setIsLoading(true); // Temporary loader dikhane ke liye

    try {
      // ⚡ AI Mapping API ko call karna zaroori hai taaki 'mapping' state update ho
      const response = await fetch('/api/inventory/map', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ headers }),
      });
      const result = await response.json();
      
      if (result.success) {
        setMapping(result.mapping); // ✅ Ab mapping null nahi rahegi!
        console.log("AI Mapping Success:", result.mapping);
      } else {
        alert("AI Mapping failed. Please check headers.");
      }
    } catch (error) {
      console.error("Mapping Error:", error);
    } finally {
      setIsLoading(false);
    }
  };

  return (
    <div className="h-[calc(100vh-75px)] w-full max-w-7xl mx-auto p-4 md:p-6 flex flex-col gap-4 overflow-hidden bg-background">
      
      <header className="flex flex-col md:flex-row justify-between items-start md:items-center gap-4 shrink-0">
        <div>
          <h1 className="text-2xl font-black tracking-tight flex items-center gap-2">
            Inventory <span className="text-[10px] bg-accent/10 text-accent px-2 py-0.5 rounded-full uppercase">Command Center</span>
          </h1>
          <p className="text-muted-foreground text-xs uppercase tracking-widest font-semibold opacity-60">Manage Real Estate Assets</p>
        </div>
        
        <div className="flex bg-muted/30 p-1 rounded-xl border border-border shrink-0">
          <button onClick={() => setActiveTab('view')} className={`px-4 py-1.5 rounded-lg text-xs cursor-pointer font-bold transition-all ${activeTab === 'view' ? 'bg-background shadow-md text-foreground' : 'text-muted-foreground'}`}>
            Live Inventory ({dbInventory.length})
          </button>
          <button onClick={() => setActiveTab('upload')} className={`px-4 py-1.5 rounded-lg text-xs font-bold cursor-pointer transition-all ${activeTab === 'upload' ? 'bg-background shadow-md text-foreground' : 'text-muted-foreground'}`}>
            Import New
          </button>
        </div>
      </header>

      {activeTab === 'view' && (
        <div className="flex gap-3 items-center shrink-0">
          <div className="relative flex-1">
            <Search className="absolute left-3 top-1/2 -translate-y-1/2 text-muted-foreground" size={16} />
            <input 
              type="text" 
              placeholder="Filter by building, unit or type..." 
              className="w-full pl-10 pr-4 py-2 bg-muted/20 border border-border rounded-xl focus:ring-1 focus:ring-foreground/20 outline-none text-sm transition-all"
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value.toLowerCase())}
            />
          </div>
          <button className="p-2 border border-border rounded-xl hover:bg-muted shrink-0"><Filter size={18} /></button>
        </div>
      )}

      <main className="flex-1 min-h-0 bg-background border border-border rounded-2xl shadow-sm flex flex-col overflow-hidden">
        {activeTab === 'upload' && excelData.length === 0 ? (
          <div className="h-full overflow-y-auto"><InventoryUpload onDataExtracted={handleDataExtracted} /></div>
        ) : (
          <div className="flex flex-col h-full overflow-hidden">
            <div className="px-6 py-3 border-b border-border bg-muted/10 flex justify-between items-center shrink-0">
              <div className="text-[10px] font-black uppercase tracking-widest text-muted-foreground flex items-center gap-2">
                <div className={`w-2 h-2 rounded-full ${activeTab === 'upload' ? 'bg-yellow-500 animate-pulse' : 'bg-green-500'}`} />
                {activeTab === 'upload' ? 'Review mode' : 'Synchronized with Cloud'}
              </div>
              {activeTab === 'upload' && (
                <button 
                  onClick={handlePushToDB} 
                  disabled={isPushing || !mapping} // Mapping jab tak nahi aati, disabled rakho
                  className="px-5 py-1.5 bg-foreground text-background rounded-lg font-bold text-xs hover:opacity-90 disabled:opacity-50 flex items-center gap-2"
                >
                  {isPushing ? (
                    <><Loader2 size={14} className="animate-spin" /> Syncing...</>
                  ) : !mapping ? (
                    "AI Mapping Headers..."
                  ) : (
                    "Confirm Sync"
                  )}
                </button>
                
              )}
            </div>

            <div className="flex-1 overflow-auto custom-scrollbar">
              {isLoading && activeTab === 'view' ? (
                <div className="h-full flex flex-col items-center justify-center gap-2 text-muted-foreground">
                  <Loader2 className="animate-spin" size={32} />
                  <p className="text-[10px] font-black uppercase tracking-widest">Fetching Assets...</p>
                </div>
              ) : (
                <table className="w-full text-left border-collapse min-w-200">
                  <thead className="sticky top-0 bg-background/95 backdrop-blur-sm z-20 shadow-sm">
                    <tr className="text-muted-foreground text-[10px] uppercase font-black tracking-widest">
                      <th className="p-5 border-b border-border">Asset / Project</th>
                      <th className="p-5 border-b border-border">Valuation</th>
                      <th className="p-5 border-b border-border">Specs</th>
                      <th className="p-5 border-b border-border text-center">Status</th>
                      <th className="p-5 border-b border-border text-right">Actions</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-border/50">
                    {(activeTab === 'view' ? filteredData : excelData).map((row, idx) => {
                      
                      // --- CASE A: PREVIEW MODE (EXCEL DATA) ---
                      if (activeTab === 'upload') {
                        // Mapping se Excel ka sahi column dhoondne ka logic
                        const getExcelVal = (systemKey: string) => {
                          const excelHeader = Object.keys(mapping || {}).find(key => mapping[key] === systemKey);
                          return excelHeader ? row[excelHeader] : '—';
                        };

                        const projectHeader = Object.keys(mapping || {}).find(key => mapping[key] === 'project_name');
                        const projectName = projectHeader ? row[projectHeader] : 'GENERIC LISTING';

                        return (
                          <tr key={`preview-${idx}`} className="group bg-muted/5 transition-colors">
                            <td className="p-5">
                              <div className="text-[10px] text-accent font-black uppercase tracking-widest mb-1">{projectName}</div>
                              <div className="font-bold text-sm text-foreground flex items-center gap-2">
                                <span className="text-muted-foreground font-medium text-xs">Unit:</span> 
                                {getExcelVal('unit_name')}
                              </div>
                            </td>
                            <td className="p-5">
                              <div className="text-sm font-black text-foreground">₹{getExcelVal('price')}</div>
                              <div className="text-[10px] text-muted-foreground font-bold uppercase tracking-widest">{getExcelVal('carpet_area')} SQFT</div>
                            </td>
                            <td className="p-5">
                              <div className="text-xs font-bold text-foreground/80">{getExcelVal('configuration')}</div>
                              <div className="text-[10px] text-muted-foreground font-medium uppercase text-xs tracking-tighter">Floor {getExcelVal('floor_no')}</div>
                            </td>
                            <td className="p-5 text-center">
                              <span className="px-3 py-1 rounded-md text-[9px] font-black uppercase tracking-widest bg-yellow-500/10 text-yellow-600 border border-yellow-500/20">
                                Reviewing
                              </span>
                            </td>
                            <td className="p-5 text-right opacity-30 italic text-[10px]">Ready to Sync</td>
                          </tr>
                        );
                      }

                      // --- CASE B: VIEW MODE (DATABASE DATA) ---
                      return (
                        <tr key={row.id || idx} className="group hover:bg-muted/10 transition-colors">
                          <td className="p-5">
                            <div className="text-[10px] text-accent font-black uppercase tracking-widest mb-1">
                              {row.properties?.name || 'GENERIC LISTING'}
                            </div>
                            <div className="font-bold text-sm text-foreground flex items-center gap-2">
                              <span className="text-muted-foreground font-medium text-xs">Unit:</span> 
                              {row.unit_name || '—'}
                            </div>
                          </td>
                          <td className="p-5">
                            <div className="text-sm font-black text-foreground">
                              {row.price ? `₹${Number(row.price).toLocaleString('en-IN')}` : '—'}
                            </div>
                            <div className="text-[10px] text-muted-foreground font-bold uppercase tracking-widest">{row.carpet_area || 0} SQFT</div>
                          </td>
                          <td className="p-5">
                            <div className="text-xs font-bold text-foreground/80">{row.configuration || '—'}</div>
                            <div className="text-[10px] text-muted-foreground font-medium uppercase">Floor {row.floor_no || 0}</div>
                          </td>
                          <td className="p-5 text-center">
                            <button 
                              onClick={() => handleToggleStatus(row.id, row.status)}
                              className={`px-3 py-1 rounded-md text-[9px] font-black uppercase tracking-widest transition-all ${
                                row.status === 'Available' 
                                ? 'bg-green-500/10 text-green-500 border border-green-500/20' 
                                : 'bg-red-500/10 text-red-500 border border-red-500/20'
                              }`}
                            >
                              {row.status || 'Available'}
                            </button>
                          </td>
                          <td className="p-5 text-right">
                            <div className="flex justify-end gap-2 md:opacity-0 group-hover:opacity-100 transition-opacity">
                              <button onClick={() => handleEditPrice(row.id, row.price)} className="p-2 hover:bg-accent/10 rounded-lg text-muted-foreground hover:text-accent transition-colors"><Edit3 size={14} /></button>
                              <button onClick={() => handleDelete(row.id)} className="p-2 hover:bg-red-500/10 rounded-lg text-muted-foreground hover:text-red-500 transition-colors"><Trash2 size={14} /></button>
                            </div>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              )}
            </div>
          </div>
        )}
      </main>
    </div>
  );
}