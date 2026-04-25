import type { Metadata } from "next";
import "./globals.css";
import Sidebar from "@/components/Sidebar";
import Header from "@/components/Header";
import { ThemeProvider } from "@/components/ThemeProvider";

export const metadata: Metadata = {
  title: "PropClose.ai | Dashboard",
  description: "AI-Powered Real Estate Assistant",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en" suppressHydrationWarning>
      <body className="flex min-h-screen bg-background text-foreground antialiased transition-colors duration-300">
        <ThemeProvider attribute="class" defaultTheme="dark" enableSystem>
          <Sidebar />
          
          {/* 1. We wrap the rest of the app in a flex-col container */}
          <div className="flex-1 flex flex-col overflow-y-auto">
            
            {/* 2. We place our new Header at the top */}
            <Header />
            
            {/* 3. The main content (page.tsx) goes below it */}
            <main className="flex-1 p-8 xl:p-12">
              {children}
            </main>
          </div>
        </ThemeProvider>
      </body>
    </html>
  );
}