"use client";

import { use } from "react";
import { AssessmentShell } from "@/components/AssessmentShell";
import { InputsSection } from "@/components/settings/InputsSection";
import { ModelsSection } from "@/components/settings/ModelsSection";

export default function SettingsPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const aid = parseInt(id);
  return (
    <AssessmentShell id={aid}>
      <h2 className="text-xl font-bold mb-1">Settings</h2>
      <p className="text-sm text-ink-600 mb-6 max-w-3xl">
        Assessment-level inputs and model routing for this assessment. Inputs shape every prompt; model routing only
        changes which model runs each stage.
      </p>
      <div className="max-w-3xl space-y-6">
        <InputsSection assessmentId={aid} />
        <ModelsSection assessmentId={aid} />
      </div>
    </AssessmentShell>
  );
}
