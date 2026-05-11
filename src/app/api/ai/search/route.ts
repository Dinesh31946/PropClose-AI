import { createClient } from '@supabase/supabase-js';
import { NextResponse } from 'next/server';
import { requireAdmin } from '@/lib/admin-auth';
import { getOpenAIClient } from '@/lib/openai';
const supabase = createClient(
  process.env.NEXT_PUBLIC_SUPABASE_URL!,
  process.env.SUPABASE_SERVICE_ROLE_KEY!
);

export async function POST(req: Request) {
  try {
    const authError = requireAdmin(req);
    if (authError) return authError;

    const openai = getOpenAIClient();
    const { query } = await req.json();

    // 1. Vector Generation
    const embeddingResponse = await openai.embeddings.create({
      model: "text-embedding-3-small",
      input: query,
    });
    const [{ embedding: queryEmbedding }] = embeddingResponse.data;

    console.log("--- STARTING DIAGNOSTIC SEARCH ---");

    // 2. Individual Call for Units
    const { data: units, error: unitError } = await supabase.rpc('match_units', {
      query_embedding: queryEmbedding,
      match_threshold: 0.2,
      match_count: 5,
    });

    if (unitError) {
      console.error("❌ UNIT ERROR:", unitError);
      return NextResponse.json({ success: false, step: "units", error: unitError.message, details: unitError.hint });
    }

    // 3. Individual Call for Chunks
    const { data: chunks, error: chunkError } = await supabase.rpc('match_chunks', {
      query_embedding: queryEmbedding,
      match_threshold: 0.2,
      match_count: 5,
    });

    if (chunkError) {
      console.error("❌ CHUNK ERROR:", chunkError);
      return NextResponse.json({ success: false, step: "chunks", error: chunkError.message, details: chunkError.hint });
    }

    // --- ONLY THIS PART MODIFIED FOR COST OPTIMIZATION ---
    // Mapping inventory to essential lines
    const cleanUnits = units?.map((u: any) => 
      `- Unit ${u.unit_name}: ${u.ai_summary}`
    ).join('\n');

    // Mapping brochure chunks to clean text (removing extra breaks and long strings)
    const cleanChunks = chunks?.map((c: any) => 
      `- Info: ${c.content.replace(/[\r\n\t\-]+/g, ' ').substring(0, 500)}...`
    ).join('\n');

    return NextResponse.json({ 
      success: true, 
      results: { 
        units_count: units?.length || 0, 
        chunks_count: chunks?.length || 0, 
        // Sending both for your testing: raw data and optimized context
        data: { units, chunks },
        contextForAI: `INVENTORY MATCHES:\n${cleanUnits}\n\nBROCHURE KNOWLEDGE:\n${cleanChunks}`
      } 
    });
    // --- END OF MODIFICATION ---

  } catch (error: any) {
    return NextResponse.json({ success: false, error: error.message }, { status: 500 });
  }
}