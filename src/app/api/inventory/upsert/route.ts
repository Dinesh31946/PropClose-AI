import { createClient } from '@supabase/supabase-js';
import { NextResponse } from 'next/server';
import OpenAI from 'openai';

const openai = new OpenAI({ apiKey: process.env.OPENAI_API_KEY });

const formatPrice = (value: any, headerName: string): string | null => {
  if (value === null || value === undefined || value === "") return null;
  let strValue = value.toString().toLowerCase().replace(/,/g, '').trim();
  let multiplier = 1;
  if (strValue.includes('cr') || strValue.includes('crore')) multiplier = 10000000;
  else if (strValue.includes('l') || strValue.includes('lac') || strValue.includes('lakh')) multiplier = 100000;
  else {
    const header = headerName.toLowerCase();
    if (header.includes('lakh') || header.includes('lac')) multiplier = 100000;
    else if (header.includes('cr') || header.includes('crore')) multiplier = 10000000;
  }
  let numericMatch = strValue.match(/\d+(\.\d+)?/);
  if (!numericMatch) return null;
  return Math.round(parseFloat(numericMatch[0]) * multiplier).toString();
};

export async function POST(req: Request) {
  try {
    const supabaseAdmin = createClient(
      process.env.NEXT_PUBLIC_SUPABASE_URL!,
      process.env.SUPABASE_SERVICE_ROLE_KEY!
    );

    const { data: rawData, mapping } = await req.json();

    const { data: properties, error: propError } = await supabaseAdmin
      .from('properties')
      .select('id, name');

    if (propError) throw propError;

    // --- 1. Data Transformation & AI Summary Generation ---
    const transformedData = rawData.map((row: any) => {
      const projectKey = Object.keys(mapping).find(k => mapping[k] === 'project_name');
      const excelProjectName = projectKey ? row[projectKey]?.toString().trim() : null;

      let matchedProp = properties?.find(p => {
        const dbName = (p.name || "").toLowerCase().replace(/\s/g, '');
        const excelName = (excelProjectName || "").toLowerCase().replace(/\s/g, '');
        return dbName.includes(excelName) || excelName.includes(dbName);
      });

      const projectId = matchedProp?.id || null;
      const unitName = row[Object.keys(mapping).find(k => mapping[k] === 'unit_name') as string] || null;
      const price = formatPrice(row[Object.keys(mapping).find(k => mapping[k] === 'price') as string], Object.keys(mapping).find(k => mapping[k] === 'price') as string);
      const config = row[Object.keys(mapping).find(k => mapping[k] === 'configuration') as string] || null;

      // Create AI Summary for Embedding
      const aiSummary = `Project: ${matchedProp?.name || excelProjectName || 'Unknown'}. Unit: ${unitName}. Price: ₹${price}. Config: ${config}. Area: ${row[Object.keys(mapping).find(k => mapping[k] === 'carpet_area') as string]} sqft.`;

      return {
        project_id: projectId,
        unit_name: unitName,
        floor_no: row[Object.keys(mapping).find(k => mapping[k] === 'floor_no') as string] || null,
        configuration: config,
        carpet_area: row[Object.keys(mapping).find(k => mapping[k] === 'carpet_area') as string]?.toString() || null,
        price: price,
        status: row[Object.keys(mapping).find(k => mapping[k] === 'status') as string] || 'Available',
        ai_summary: aiSummary,
        metadata: {
          listing_type: excelProjectName ? 'Project Based' : 'Individual Listing',
          original_project_name: excelProjectName || 'Unknown'
        }
      };
    });

    // --- 2. AI Embedding Generation (In Batches for Speed) ---
    // Extract summaries to send to OpenAI
    const summaries = transformedData.map((d: any) => d.ai_summary);
    
    // OpenAI supports batch embedding
    const embeddingResponse = await openai.embeddings.create({
      model: "text-embedding-3-small",
      input: summaries,
    });

    // Map embeddings back to data
    const finalData = transformedData.map((item: any, index: number) => ({
      ...item,
      embedding: embeddingResponse.data[index].embedding
    }));

    // --- 3. Final Bulk Insert ---
    const { error: insertError } = await supabaseAdmin
      .from('unit_inventory')
      .insert(finalData);

    if (insertError) throw insertError;

    return NextResponse.json({ success: true, count: finalData.length });
  } catch (error: any) {
    console.error("Upsert/Embed Error:", error.message);
    return NextResponse.json({ success: false, error: error.message }, { status: 500 });
  }
}