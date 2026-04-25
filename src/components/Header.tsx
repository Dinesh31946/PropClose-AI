"use client"
import { useTheme } from "next-themes";
import { Sun, Moon, Bell } from "lucide-react";
import { useEffect, useState } from "react";

export default function Header() {
  const { theme, setTheme } = useTheme();
  const [mounted, setMounted] = useState(false);

  // Prevent flicker on mount
  useEffect(() => setMounted(true), []);

  if (!mounted) return null;

  return (
    <header className="sticky top-0 z-40 w-full border-b border-border bg-background/80 backdrop-blur-md">
      <div className="flex h-16 items-center justify-between px-8">
        {/* We keep this left side empty for now (could hold a Search Bar later) */}
        <div></div>

        {/* This is the Utility Section where you marked */}
        <div className="flex items-center gap-2">
          
          {/* A small notification bell always looks pro */}
          <button className="p-2.5 rounded-lg text-neutral-500 hover:text-foreground hover:bg-neutral-100 dark:hover:bg-neutral-900 transition-all">
            <Bell size={18} />
          </button>

          {/* THE THEME TOGGLE BUTTON (Tucked away where you marked) */}
          <button 
            onClick={() => setTheme(theme === "dark" ? "light" : "dark")}
            className="p-2.5 rounded-lg text-neutral-500 hover:text-foreground hover:bg-neutral-100 dark:hover:bg-neutral-900 transition-all"
          >
            {theme === "dark" ? <Sun size={18} /> : <Moon size={18} />}
          </button>
        </div>
      </div>
    </header>
  );
}