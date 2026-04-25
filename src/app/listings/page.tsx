"use client"
import { useState, useEffect } from "react";
import PropertyCard from "@/components/PropertyCard";
import AddPropertyModal from "@/components/AddPropertyModal";
import { createClient } from "@/lib/supabase";
import { Home } from "lucide-react";

export default function ListingsPage() {
  const [isModalOpen, setIsModalOpen] = useState(false);
  const [properties, setProperties] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);

  // Fetch real listings from Supabase
  const fetchProperties = async () => {
    const supabase = createClient();
    const { data, error } = await supabase
      .from('properties')
      .select('*')
      .order('created_at', { ascending: false });

    if (data) setProperties(data);
    setLoading(false);
  };

  useEffect(() => {
    fetchProperties();
  }, []);

  return (
  <div className="space-y-8">
    <div className="flex items-center justify-between">
      <div>
        <h1 className="text-3xl font-bold tracking-tight">Property Listings</h1>
        <p className="text-neutral-500">
          {loading ? "Loading your properties..." : `Total Listings: ${properties.length}`}
        </p>
      </div>
      
      {properties.length > 0 && (
        <button 
          onClick={() => setIsModalOpen(true)}
          className="bg-accent text-white px-6 py-2.5 rounded-lg font-medium hover:bg-accent/80 transition-all"
        >
          + New Property
        </button>
      )}
    </div>

    {loading ? (
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
        {[1, 2, 3].map((i) => (
          <div key={i} className="h-64 bg-neutral-100 dark:bg-neutral-900/50 animate-pulse rounded-2xl border border-border"></div>
        ))}
      </div>
    ) : properties.length === 0 ? (
      /* --- THIS IS YOUR NEW PROFESSIONAL EMPTY STATE --- */
      <div className="flex flex-col items-center justify-center py-20 border-2 border-dashed border-border rounded-3xl bg-neutral-50/50 dark:bg-neutral-900/20">
        <div className="w-16 h-16 bg-accent/10 rounded-full flex items-center justify-center mb-4">
          <Home className="text-accent" size={32} />
        </div>
        <h3 className="text-xl font-semibold">No properties yet</h3>
        <p className="text-neutral-500 mt-2 max-w-sm text-center">
          Upload your first property brochure to start training your AI assistant.
        </p>
        <button 
          onClick={() => setIsModalOpen(true)}
          className="mt-6 bg-accent text-white px-8 py-3 rounded-xl font-bold hover:scale-105 transition-all shadow-lg shadow-accent/20"
        >
          Add Your First Property
        </button>
      </div>
    ) : (
      /* --- SHOW ACTUAL LISTINGS --- */
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
        {properties.map((prop) => (
          <PropertyCard 
            key={prop.id} 
            name={prop.name} 
            location={prop.location || "Location Pending"} 
            price={prop.price || "Contact for Price"} 
            type="Residential" 
          />
        ))}
      </div>
    )}

    <AddPropertyModal 
      isOpen={isModalOpen} 
      onClose={() => {
        setIsModalOpen(false);
        fetchProperties();
      }} 
    />
  </div>
);
}