"use client";

import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { LeftNav } from "./LeftNav";
import { ModelPicker } from "./ModelPicker";
import { EvidenceDrawerProvider } from "./EvidenceDrawer";

export function AssessmentShell({ id, children }: { id: number; children: React.ReactNode }) {
  const { data: assessment, isLoading } = useQuery({
    queryKey: ["assessment", id],
    queryFn: () => api.getAssessment(id),
  });

  if (isLoading || !assessment) {
    return <div className="p-8 text-ink-500">Loading…</div>;
  }

  return (
    <EvidenceDrawerProvider assessmentId={id}>
      <div className="flex min-h-screen">
        <LeftNav
          assessmentId={id}
          vendorName={assessment.vendor_name}
          currentPhase={assessment.current_phase}
        />
        <main className="flex-1 min-w-0">
          <header className="px-8 py-4 border-b border-ink-200 bg-white flex items-center justify-between gap-4">
            <div>
              <div className="text-xs uppercase tracking-wide text-ink-500">Vendor</div>
              <div className="text-base font-semibold text-ink-900">{assessment.vendor_name}</div>
            </div>
            <ModelPicker assessmentId={id} overrides={assessment.model_overrides || {}} />
          </header>
          <div className="px-8 py-6 max-w-[1400px]">{children}</div>
        </main>
      </div>
    </EvidenceDrawerProvider>
  );
}
