'use client';

import { FormEvent, useCallback, useEffect, useMemo, useState } from 'react';
import { createClient } from '@supabase/supabase-js';
import { AlertCircle, Loader2 } from 'lucide-react';
import { createLead } from '@/lib/leadsApi';

type LeadRow = {
  id: string;
  name: string | null;
  phone: string | null;
  email: string | null;
  source: string | null;
  status: string | null;
  property_id: string | null;
  created_at?: string | null;
  properties?: { name?: string | null } | null;
};

const supabase = createClient(
  process.env.NEXT_PUBLIC_SUPABASE_URL!,
  process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY!
);

const initialForm = {
  name: '',
  phone: '',
  email: '',
  source: 'Website',
  property_name: '',
};

export default function LeadsPage() {
  const [form, setForm] = useState(initialForm);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [isLoadingLeads, setIsLoadingLeads] = useState(true);
  const [leads, setLeads] = useState<LeadRow[]>([]);
  const [statusMessage, setStatusMessage] = useState<string | null>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  const fetchLeads = useCallback(async () => {
    setIsLoadingLeads(true);
    try {
      const { data, error } = await supabase
        .from('leads')
        .select('id,name,phone,email,source,status,property_id,created_at,properties(name)')
        .order('created_at', { ascending: false })
        .limit(100);
      if (error) throw error;
      setLeads((data || []) as LeadRow[]);
    } catch (error: any) {
      setErrorMessage(error?.message || 'Unable to fetch leads.');
    } finally {
      setIsLoadingLeads(false);
    }
  }, []);

  useEffect(() => {
    fetchLeads();
  }, [fetchLeads]);

  const phonePreview = useMemo(() => form.phone.replace(/[\s\-()]/g, ''), [form.phone]);

  const handleSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setIsSubmitting(true);
    setErrorMessage(null);
    setStatusMessage(null);

    try {
      const response = await createLead({
        name: form.name,
        phone: form.phone,
        email: form.email || undefined,
        source: form.source || undefined,
        property_name: form.property_name || undefined,
      });

      // Backend duplicate guard ke hisaab se UX message differentiate karte hain.
      setStatusMessage(
        response.duplicate
          ? 'Duplicate lead mila, naya row banane ke bajay timestamp refresh hua.'
          : 'Lead create ho gaya. Welcome automation background me trigger ho chuka hai.'
      );
      setForm(initialForm);
      await fetchLeads();
    } catch (error: any) {
      setErrorMessage(error?.message || 'Lead save failed.');
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <div className="h-[calc(100vh-75px)] w-full max-w-7xl mx-auto p-4 md:p-6 flex flex-col gap-4 overflow-hidden bg-background">
      <header className="shrink-0">
        <h1 className="text-2xl font-black tracking-tight">Leads Command Center</h1>
        <p className="text-muted-foreground text-xs uppercase tracking-widest font-semibold opacity-60">
          FastAPI driven lead capture with smart property linking
        </p>
      </header>

      <section className="grid grid-cols-1 lg:grid-cols-3 gap-4 shrink-0">
        <form onSubmit={handleSubmit} className="lg:col-span-1 border border-border rounded-2xl p-4 space-y-3 bg-background">
          <h2 className="text-sm font-black uppercase tracking-widest">Add Lead</h2>

          <input
            className="w-full px-3 py-2 text-sm rounded-xl border border-border bg-muted/20"
            placeholder="Lead name"
            value={form.name}
            onChange={(event) => setForm((prev) => ({ ...prev, name: event.target.value }))}
            required
          />
          <input
            className="w-full px-3 py-2 text-sm rounded-xl border border-border bg-muted/20"
            placeholder="Phone number"
            value={form.phone}
            onChange={(event) => setForm((prev) => ({ ...prev, phone: event.target.value }))}
            required
          />
          <p className="text-[10px] uppercase tracking-widest text-muted-foreground">
            Normalized phone preview: {phonePreview || '—'}
          </p>
          <input
            className="w-full px-3 py-2 text-sm rounded-xl border border-border bg-muted/20"
            placeholder="Email (optional)"
            type="email"
            value={form.email}
            onChange={(event) => setForm((prev) => ({ ...prev, email: event.target.value }))}
          />
          <input
            className="w-full px-3 py-2 text-sm rounded-xl border border-border bg-muted/20"
            placeholder="Source (optional)"
            value={form.source}
            onChange={(event) => setForm((prev) => ({ ...prev, source: event.target.value }))}
          />
          <input
            className="w-full px-3 py-2 text-sm rounded-xl border border-border bg-muted/20"
            placeholder="Property name for smart linking"
            value={form.property_name}
            onChange={(event) => setForm((prev) => ({ ...prev, property_name: event.target.value }))}
          />

          <button
            type="submit"
            disabled={isSubmitting}
            className="w-full px-4 py-2 rounded-xl text-xs font-black uppercase tracking-widest bg-foreground text-background disabled:opacity-60 flex items-center justify-center gap-2"
          >
            {isSubmitting ? <><Loader2 size={14} className="animate-spin" /> Saving…</> : 'Create Lead'}
          </button>
        </form>

        <div className="lg:col-span-2 border border-border rounded-2xl overflow-hidden bg-background min-h-[360px]">
          <div className="px-4 py-3 border-b border-border text-[10px] font-black uppercase tracking-widest text-muted-foreground">
            Recent Leads
          </div>
          {isLoadingLeads ? (
            <div className="h-full min-h-[300px] flex items-center justify-center text-muted-foreground">
              <Loader2 size={22} className="animate-spin" />
            </div>
          ) : (
            <div className="overflow-auto max-h-[420px]">
              <table className="w-full text-left text-sm">
                <thead className="sticky top-0 bg-background z-10">
                  <tr className="text-[10px] uppercase tracking-widest text-muted-foreground">
                    <th className="px-4 py-3 border-b border-border">Lead</th>
                    <th className="px-4 py-3 border-b border-border">Phone</th>
                    <th className="px-4 py-3 border-b border-border">Property</th>
                    <th className="px-4 py-3 border-b border-border">Status</th>
                  </tr>
                </thead>
                <tbody>
                  {leads.map((lead) => (
                    <tr key={lead.id} className="border-b border-border/60">
                      <td className="px-4 py-3">
                        <div className="font-semibold">{lead.name || 'Unknown'}</div>
                        <div className="text-xs text-muted-foreground">{lead.email || 'No email'}</div>
                      </td>
                      <td className="px-4 py-3">{lead.phone || '—'}</td>
                      <td className="px-4 py-3">{lead.properties?.name || 'Unlinked'}</td>
                      <td className="px-4 py-3">{lead.status || 'New'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </section>

      {(statusMessage || errorMessage) && (
        <div
          className={`flex items-center gap-2 text-xs px-3 py-2 rounded-xl border ${
            errorMessage
              ? 'text-red-500 bg-red-500/5 border-red-500/30'
              : 'text-green-600 bg-green-500/5 border-green-500/30'
          }`}
        >
          <AlertCircle size={14} />
          <span>{errorMessage || statusMessage}</span>
        </div>
      )}
    </div>
  );
}

