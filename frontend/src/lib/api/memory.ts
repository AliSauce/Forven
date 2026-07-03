import { fetchApi } from './core';

// The Memory Bank page/backend was removed 2026-07-02 (never used). This module
// now only carries the quant-skills client, which backs the learned-skills KB.

// ── Quant Skills Types ──────────────────────────────────────────────────────

export interface QuantSkill {
	name: string;
	description: string;
	skill_type: 'regime' | 'failure' | 'indicator' | 'combo' | 'params';
	confidence: number;
	sample_size: number;
	regime: string;
	last_validated: string;
	what_works: string[];
	what_doesnt_work: string[];
	evidence: Record<string, unknown>[];
	metadata: Record<string, string>;
}

export interface SkillCandidateHypothesis {
	id: string;
	pattern: string;
	observation: string;
	backtest_ids: string[];
	created_at: string;
	count: number;
}

export interface QuantSkillsStats {
	total_skills: number;
	total_hypotheses: number;
	total_archived: number;
	avg_confidence: number;
	total_evidence: number;
}

// ── Quant Skills API ────────────────────────────────────────────────────────

export async function getQuantSkills(params?: {
	regime?: string;
	skill_type?: string;
	limit?: number;
	min_confidence?: number;
}): Promise<{ skills: QuantSkill[]; meta: Record<string, unknown> }> {
	const qs = new URLSearchParams();
	if (params?.regime) qs.set('regime', params.regime);
	if (params?.skill_type) qs.set('skill_type', params.skill_type);
	if (params?.limit) qs.set('limit', String(params.limit));
	if (params?.min_confidence !== undefined) qs.set('min_confidence', String(params.min_confidence));
	const query = qs.toString();
	return fetchApi(`/quant-skills${query ? '?' + query : ''}`);
}

export async function getQuantSkillDetail(name: string): Promise<QuantSkill> {
	return fetchApi(`/quant-skills/${encodeURIComponent(name)}`);
}

export async function getSkillCandidateHypotheses(): Promise<{ hypotheses: SkillCandidateHypothesis[] }> {
	return fetchApi('/quant-skills/hypotheses');
}

export async function promoteSkillCandidateHypothesis(id: string): Promise<{ promoted: boolean; skill_name: string }> {
	return fetchApi(`/quant-skills/hypotheses/${encodeURIComponent(id)}/promote`, { method: 'POST' });
}

export async function dismissSkillCandidateHypothesis(id: string): Promise<{ dismissed: boolean }> {
	return fetchApi(`/quant-skills/hypotheses/${encodeURIComponent(id)}`, { method: 'DELETE' });
}

export async function runConsolidation(): Promise<{ status: string; report: Record<string, number> }> {
	return fetchApi('/quant-skills/consolidation', { method: 'POST' });
}

export async function getQuantSkillsStats(): Promise<QuantSkillsStats> {
	return fetchApi('/quant-skills/stats');
}

export async function archiveSkill(name: string): Promise<{ archived: boolean }> {
	return fetchApi(`/quant-skills/${encodeURIComponent(name)}`, { method: 'DELETE' });
}
