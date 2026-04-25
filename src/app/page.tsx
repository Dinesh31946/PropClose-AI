import LeadsTable from "@/components/LeadsTable";

export default function Home() {
  return (
    <div className="w-full max-w-[1600px] mx-auto space-y-12">
      {/* Page Title Section */}
      <div className="space-y-1">
        <h1 className="text-3xl font-semibold tracking-tight text-foreground">
          Welcome back, Dinesh
        </h1>
        <p className="text-neutral-500">
          Here is your real estate lead snapshot for today.
        </p>
      </div>

      {/* Leads Section */}
      <div className="space-y-4">
        <div className="flex items-center justify-between">
          <h2 className="text-lg font-medium">Recent Leads</h2>
          <button className="text-sm bg-accent hover:bg-accent/80 text-white px-4 py-2 rounded-lg transition-colors">
            + Add Lead
          </button>
        </div>
        
        <LeadsTable />
      </div>
    </div>
  );
}