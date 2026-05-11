import { createClient } from '@supabase/supabase-js';
import { NextResponse } from 'next/server';
import OpenAI from 'openai';
import { requireAdmin } from '@/lib/admin-auth';

const openai = new OpenAI({ apiKey: process.env.OPENAI_API_KEY });
const supabase = createClient(
  process.env.NEXT_PUBLIC_SUPABASE_URL!,
  process.env.SUPABASE_SERVICE_ROLE_KEY! // Admin key chahiye update karne ke liye
);

export async function POST(req: Request) {
  try {
    const authError = requireAdmin(req);
    if (authError) return authError;

    // 1. Database se wo units uthao jinka embedding abhi tak nahi bana hai
    const { data: units, error: fetchError } = await supabase
      .from('unit_inventory')
      .select('*, properties(name)')
      .is('embedding', null);

    if (fetchError) throw fetchError;
    if (!units || units.length === 0) {
      return NextResponse.json({ message: "All units are already activated!" });
    }

    console.log(`Processing ${units.length} units...`);

    // 2. Har unit ke liye AI summary banao aur embed karo
    for (const unit of units) {
      const projectName = unit.properties?.name || "Unknown Project";
      
      // Ye hai "Accuracy" ka secret - AI ko context dena
      const aiContent = `Project: ${projectName}. Unit: ${unit.unit_name}. Configuration: ${unit.configuration}. Price: ₹${unit.price}. Carpet Area: ${unit.carpet_area} sqft. Floor: ${unit.floor_no}. Status: ${unit.status}.`;

      // OpenAI se Vector/Embedding maango
      const embeddingResponse = await openai.embeddings.create({
        model: "text-embedding-3-small",
        input: aiContent,
      });

      const [{ embedding }] = embeddingResponse.data;

      // 3. Database mein wapas save karo
      const { error: updateError } = await supabase
        .from('unit_inventory')
        .update({ 
          embedding: embedding,
          ai_summary: aiContent 
        })
        .eq('id', unit.id);

      if (updateError) console.error(`Error updating unit ${unit.id}:`, updateError);
    }

    return NextResponse.json({ 
      success: true, 
      message: `Successfully activated ${units.length} units!` 
    });

  } catch (error: any) {
    console.error("Embedding Error:", error);
    return NextResponse.json({ success: false, error: error.message }, { status: 500 });
  }
}