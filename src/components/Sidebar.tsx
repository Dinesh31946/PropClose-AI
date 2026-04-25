"use client"
import { LayoutDashboard, Users, Home, Settings, MessageSquare } from "lucide-react";
import Link from "next/link"; // 1. Import Link for navigation
import { usePathname } from "next/navigation"; // 2. Import hook to check current page
import { useEffect, useState } from "react";

export default function Sidebar() {
  const [mounted, setMounted] = useState(false);
  const pathname = usePathname(); // This tells us if we are on "/" or "/listings"

  useEffect(() => setMounted(true), []);

  const menuItems = [
    { icon: <LayoutDashboard size={20} />, label: "Dashboard", path: "/" },
    { icon: <MessageSquare size={20} />, label: "Conversations", path: "/conversations" },
    { icon: <Users size={20} />, label: "Leads", path: "/leads" },
    { icon: <Home size={20} />, label: "Listings", path: "/listings" },
    { icon: <Settings size={20} />, label: "Settings", path: "/settings" },
  ];

  if (!mounted) return null;

  return (
    <aside className="w-64 border-r border-border bg-background flex flex-col h-screen sticky top-0">
      <div className="p-6">
        <h2 className="text-xl font-bold tracking-tighter">PropClose<span className="text-accent">.ai</span></h2>
      </div>
      
      <nav className="flex-1 px-4 space-y-2">
        {menuItems.map((item) => {
          // 3. Check if this specific item is the page we are currently on
          const isActive = pathname === item.path;

          return (
            <Link
              key={item.label}
              href={item.path} // 4. Tell it where to go
              className={`flex items-center gap-3 px-4 py-3 rounded-lg transition-all ${
                isActive 
                  ? "bg-accent/10 text-accent" 
                  : "text-neutral-400 hover:bg-neutral-900/10 hover:text-foreground"
              }`}
            >
              {item.icon}
              <span className="font-medium">{item.label}</span>
            </Link>
          );
        })}
      </nav>

      <div className="p-4 border-t border-border">
        <div className="flex items-center gap-3 px-2">
          <div className="w-8 h-8 rounded-full bg-accent flex items-center justify-center text-xs font-bold text-white uppercase">DG</div>
          <div className="text-sm text-foreground">
            <p className="font-medium">Dinesh Gosavi</p>
            <p className="text-xs text-neutral-500">Pro Plan</p>
          </div>
        </div>
      </div>
    </aside>
  );
}