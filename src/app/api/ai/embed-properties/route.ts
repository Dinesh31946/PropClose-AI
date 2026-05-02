import { createClient } from '@supabase/supabase-js';
import { NextResponse } from 'next/server';
import OpenAI from 'openai';

const openai = new OpenAI({ apiKey: process.env.OPENAI_API_KEY });
const supabase = createClient(
  process.env.NEXT_PUBLIC_SUPABASE_URL!,
  process.env.SUPABASE_SERVICE_ROLE_KEY! // Admin access for updates
);

export async function POST() {
  try {
    // 1. Fetch properties that don't have embeddings yet
    const { data: properties, error: fetchError } = await supabase
      .from('properties')
      .select('*')
      .is('embedding', null);

    if (fetchError) throw fetchError;
    if (!properties || properties.length === 0) {
      return NextResponse.json({ message: "All projects are already AI-activated!" });
    }

    console.log(`Activating ${properties.length} projects...`);

    for (const prop of properties) {
      // 2. Price Normalization (Text to Numeric)
      // Agar price '1.2 Cr' hai toh hum usey numeric mein convert karenge
      const rawPrice = prop.price || "0";
      let numericPrice = 0;
      
      if (rawPrice.toLowerCase().includes('cr')) numericPrice = parseFloat(rawPrice) * 10000000;
      else if (rawPrice.toLowerCase().includes('l')) numericPrice = parseFloat(rawPrice) * 100000;
      else numericPrice = parseFloat(rawPrice.replace(/[^0-9.]/g, '')) || 0;

      // 3. Create AI Context Summary
      const aiContent = `Project Name: ${prop.name}. Location: ${prop.location}. Description: ${prop.description}. Starting Price: ₹${numericPrice}.`;

      // 4. Generate Embedding
      const embeddingResponse = await openai.embeddings.create({
        model: "text-embedding-3-small",
        input: aiContent,
      });

      const [{ embedding }] = embeddingResponse.data;

      // 5. Update Database
      const { error: updateError } = await supabase
        .from('properties')
        .update({ 
          embedding: embedding,
          ai_summary: aiContent,
          price_numeric: numericPrice 
        })
        .eq('id', prop.id);

      if (updateError) console.error(`Failed to activate ${prop.name}:`, updateError);
    }

    return NextResponse.json({ 
      success: true, 
      message: `Successfully activated ${properties.length} projects for AI search!` 
    });

  } catch (error: any) {
    return NextResponse.json({ success: false, error: error.message }, { status: 500 });
  }
}