"use client"
import { MapPin, IndianRupee, Building2, Trash2, ExternalLink } from "lucide-react";

interface PropertyProps {
  id: string;
  name: string;
  location: string;
  price: string;
  type: string;
}

export default function PropertyCard({ id, name, location, price, type }: PropertyProps) {
  return (
    <div className="bg-background border border-border rounded-2xl overflow-hidden group hover:shadow-xl hover:shadow-accent/5 transition-all duration-300">
      {/* Property Preview Area */}
      <div className="aspect-video bg-neutral-100 dark:bg-neutral-900 flex flex-col items-center justify-center relative overflow-hidden">
        <Building2 className="text-neutral-300 dark:text-neutral-700 group-hover:scale-110 transition-transform duration-500" size={48} />
        <div className="absolute top-3 right-3">
          <span className="bg-accent/10 text-accent text-[10px] font-bold uppercase tracking-widest px-2.5 py-1 rounded-full border border-accent/20 backdrop-blur-md">
            {type}
          </span>
        </div>
      </div>

      {/* Content Area */}
      <div className="p-5 space-y-4">
        <div>
          <h3 className="text-lg font-bold text-foreground leading-tight truncate">{name}</h3>
          <div className="flex items-center gap-1.5 mt-1 text-neutral-500">
            <MapPin size={14} />
            <span className="text-xs font-medium">{location}</span>
          </div>
        </div>

        <div className="flex items-center justify-between pt-2 border-t border-border">
          <div className="flex items-center gap-1 text-accent">
            <IndianRupee size={14} />
            <span className="text-sm font-bold">{price}</span>
          </div>
          
          <div className="flex gap-2">
            <button className="p-2 hover:bg-neutral-100 dark:hover:bg-neutral-800 rounded-lg text-neutral-400 hover:text-foreground transition-colors">
              <ExternalLink size={18} />
            </button>
            <button className="p-2 hover:bg-red-50 dark:hover:bg-red-950/30 rounded-lg text-neutral-400 hover:text-red-500 transition-colors">
              <Trash2 size={18} />
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}