export default function LeadsTable() {
  const mockLeads = [
    { id: 1, name: "Dinesh Gosavi", phone: "+91 98765...", status: "New", property: "Sea View Heights" },
    { id: 2, name: "Rahul Sharma", phone: "+91 91234...", status: "Contacted", property: "Green Valley" },
  ];

  return (
    <div className="w-full overflow-hidden rounded-xl border border-border bg-background/50 backdrop-blur-md">
      <table className="w-full text-left text-sm">
        <thead className="bg-neutral-100 dark:bg-neutral-800/50 text-neutral-500 font-medium">
          <tr>
            <th className="p-4">Lead Name</th>
            <th className="p-4">Property</th>
            <th className="p-4">Status</th>
            <th className="p-4 text-right">Action</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-border">
          {mockLeads.map((lead) => (
            <tr key={lead.id} className="hover:bg-neutral-50 dark:hover:bg-neutral-800/30 transition-colors">
              <td className="p-4 font-medium">{lead.name}</td>
              <td className="p-4 text-neutral-500">{lead.property}</td>
              <td className="p-4">
                <span className="px-2 py-1 rounded-md bg-accent/10 text-accent text-[10px] font-bold uppercase">
                  {lead.status}
                </span>
              </td>
              <td className="p-4 text-right">
                <button className="text-xs text-neutral-400 hover:text-foreground">View Details</button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}