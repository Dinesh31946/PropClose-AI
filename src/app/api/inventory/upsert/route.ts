import { createClient } from '@supabase/supabase-js';
import { NextResponse } from 'next/server';

const formatPrice = (value: any, headerName: string): string | null => {
  if (value === null || value === undefined || value === "") return null;
  
  let strValue = value.toString().toLowerCase().replace(/,/g, '').trim();
  
  // 1. Check for Units INSIDE the value (e.g., "1.3Cr", "83L")
  let multiplier = 1;
  
  if (strValue.includes('cr') || strValue.includes('crore')) {
    multiplier = 10000000;
  } else if (strValue.includes('l') || strValue.includes('lac') || strValue.includes('lakh')) {
    multiplier = 100000;
  } 
  // 2. Fallback: Check Header if no units found in cell (e.g., Header: "Price in Lakhs", Value: "82")
  else {
    const header = headerName.toLowerCase();
    if (header.includes('lakh') || header.includes('lac')) multiplier = 100000;
    else if (header.includes('cr') || header.includes('crore')) multiplier = 10000000;
  }

  // 3. Extract only numbers/decimals (e.g., "1.3" from "1.3Cr")
  let numericMatch = strValue.match(/\d+(\.\d+)?/);
  if (!numericMatch) return null;

  let numericPrice = parseFloat(numericMatch[0]) * multiplier;

  return Math.round(numericPrice).toString();
};

export async function POST(req: Request) {
  try {
    const supabaseAdmin = createClient(
      process.env.NEXT_PUBLIC_SUPABASE_URL!,
      process.env.SUPABASE_SERVICE_ROLE_KEY!
    );

    const { data: rawData, mapping } = await req.json();

    // 1. Fetch All Properties (Reference Data)
    const { data: properties, error: propError } = await supabaseAdmin
      .from('properties')
      .select('id, name');

    if (propError) throw propError;

    // 2. Data Transformation with Smart Linking
    const finalData = rawData.map((row: any) => {
      // Find the project name from Excel
      const projectKey = Object.keys(mapping).find(k => mapping[k] === 'project_name');
      const excelProjectName = projectKey ? row[projectKey]?.toString().trim() : null;

      // 🔍 Startup Style Matching: Match Name
      let matchedProp = properties?.find(p => {
        const dbName = ( p.name || "").toLowerCase().replace(/\s/g, '');
        const excelName = (excelProjectName || "").toLowerCase().replace(/\s/g, '');
        return dbName.includes(excelName) || excelName.includes(dbName);
      });

      // Default Logic for Rent/Sales (If No Project Found)
      // Startup inspiration: Use a "Generic/Global" property ID if specific one is missing
      const projectId = matchedProp?.id || null;

      const transformedRow: any = {
        project_id: projectId, 
        unit_name: row[Object.keys(mapping).find(k => mapping[k] === 'unit_name') as string] || null,
        floor_no: row[Object.keys(mapping).find(k => mapping[k] === 'floor_no') as string] || null,
        configuration: row[Object.keys(mapping).find(k => mapping[k] === 'configuration') as string] || null,
        carpet_area: row[Object.keys(mapping).find(k => mapping[k] === 'carpet_area') as string]?.toString() || null,
        // price: row[Object.keys(mapping).find(k => mapping[k] === 'price') as string]?.toString() || null,
        price: formatPrice(row[Object.keys(mapping).find(k => mapping[k] === 'price') as string], Object.keys(mapping).find(k => mapping[k] === 'price') as string),
        status: row[Object.keys(mapping).find(k => mapping[k] === 'status') as string] || 'Available',
        metadata: {
          listing_type: excelProjectName ? 'Project Based' : 'Individual Listing', // Rent/Sales indicator
          original_project_name: excelProjectName || 'Unknown'
        }
      };

      // Extract all unmapped columns into Metadata (Accuracy Guard)
      Object.entries(mapping).forEach(([excelHeader, systemCol]) => {
        if (systemCol === 'metadata' || systemCol === 'project_name') {
          transformedRow.metadata[excelHeader] = row[excelHeader];
        }
      });

      return transformedRow;
    });

    // 3. Bulk Insert
    const { error: insertError } = await supabaseAdmin
      .from('unit_inventory')
      .insert(finalData);

    if (insertError) throw insertError;

    return NextResponse.json({ success: true, count: finalData.length });
  } catch (error: any) {
    return NextResponse.json({ success: false, error: error.message }, { status: 500 });
  }
}