"use client"
import { useState } from "react";
import { X, Upload, Loader2, MapPin, IndianRupee } from "lucide-react";
import { useRouter } from "next/navigation";

export default function AddPropertyModal({ isOpen, onClose }: { isOpen: boolean, onClose: () => void }) {
  const [loading, setLoading] = useState(false);
  const [formData, setFormData] = useState({
    title: "",
    location: "",
    price: "",
  });
  const [file, setFile] = useState<File | null>(null);
  const router = useRouter();

  if (!isOpen) return null;

  const handleUpload = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!file || !formData.title) return alert("Please provide Title and PDF");

    setLoading(true);
    const data = new FormData();
    data.append("file", file);
    data.append("title", formData.title);
    data.append("location", formData.location);
    data.append("price", formData.price);

    try {
      const response = await fetch("/api/ingest", { method: "POST", body: data });
      if (response.ok) {
        alert("Knowledge Base Updated Successfully!");
        router.refresh();
        onClose();
      }
    } catch (error) {
      console.error(error);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm p-4">
      <div className="bg-background border border-border w-full max-w-lg rounded-2xl p-8 shadow-2xl">
        <div className="flex justify-between items-center mb-8">
          <div>
            <h2 className="text-2xl font-bold">New Property Knowledge</h2>
            <p className="text-sm text-neutral-500">Add listing details and AI brochure.</p>
          </div>
          <button onClick={onClose} className="text-neutral-500 hover:text-foreground"><X size={24}/></button>
        </div>

        <form onSubmit={handleUpload} className="space-y-6">
          <div className="grid grid-cols-1 gap-4">
            <div>
              <label className="text-xs font-bold uppercase tracking-wider text-neutral-500 mb-2 block">Project Name</label>
              <input 
                required
                className="w-full bg-neutral-100 dark:bg-neutral-900 border border-border rounded-xl px-4 py-3 outline-none focus:ring-2 focus:ring-accent transition-all"
                placeholder="e.g. Arihant Aspire"
                onChange={(e) => setFormData({...formData, title: e.target.value})}
              />
            </div>
            
            <div className="flex gap-4">
              <div className="flex-1">
                <label className="text-xs font-bold uppercase tracking-wider text-neutral-500 mb-2 block">Location</label>
                <div className="relative">
                  <MapPin className="absolute left-3 top-3.5 text-neutral-500" size={18} />
                  <input 
                    className="w-full bg-neutral-100 dark:bg-neutral-900 border border-border rounded-xl pl-10 pr-4 py-3 outline-none focus:ring-2 focus:ring-accent transition-all"
                    placeholder="e.g. Panvel"
                    onChange={(e) => setFormData({...formData, location: e.target.value})}
                  />
                </div>
              </div>
              <div className="flex-1">
                <label className="text-xs font-bold uppercase tracking-wider text-neutral-500 mb-2 block">Starting Price</label>
                <div className="relative">
                  <IndianRupee className="absolute left-3 top-3.5 text-neutral-500" size={18} />
                  <input 
                    className="w-full bg-neutral-100 dark:bg-neutral-900 border border-border rounded-xl pl-10 pr-4 py-3 outline-none focus:ring-2 focus:ring-accent transition-all"
                    placeholder="e.g. 85L"
                    onChange={(e) => setFormData({...formData, price: e.target.value})}
                  />
                </div>
              </div>
            </div>
          </div>

          <div>
            <label className="text-xs font-bold uppercase tracking-wider text-neutral-500 mb-2 block">Knowledge Source (PDF)</label>
            <div className="border-2 border-dashed border-border rounded-2xl p-10 flex flex-col items-center justify-center space-y-3 hover:border-accent hover:bg-accent/5 transition-all cursor-pointer relative group">
              <input type="file" accept=".pdf" className="absolute inset-0 opacity-0 cursor-pointer" onChange={(e) => setFile(e.target.files?.[0] || null)} />
              <div className="w-12 h-12 rounded-full bg-neutral-100 dark:bg-neutral-800 flex items-center justify-center group-hover:scale-110 transition-transform">
                <Upload className="text-accent" size={24} />
              </div>
              <p className="text-sm font-medium text-neutral-600 dark:text-neutral-400">
                {file ? file.name : "Upload Property Brochure"}
              </p>
              <p className="text-[10px] text-neutral-500 italic">AI will extract amenities & details from this file</p>
            </div>
          </div>

          <button 
            type="submit" 
            disabled={loading}
            className="w-full bg-accent text-white py-4 rounded-xl font-bold flex items-center justify-center gap-3 hover:bg-accent/90 shadow-lg shadow-accent/20 transition-all disabled:opacity-50 cursor-pointer"
          >
            {loading && <Loader2 className="animate-spin" size={20} />}
            {loading ? "AI is processing brochure..." : "Create Listing Knowledge"}
          </button>
        </form>
      </div>
    </div>
  );
}