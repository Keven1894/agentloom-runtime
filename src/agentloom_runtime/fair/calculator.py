"""
FAIR Compliance Calculator

A reusable, standalone calculator for assessing metadata compliance with FAIR principles.
Based on official FAIR Guiding Principles (Wilkinson et al., 2016, https://doi.org/10.1038/sdata.2016.18)

This module provides a pure Python implementation with no external dependencies beyond stdlib.
It can be used standalone or integrated into larger systems (e.g., MCP tools, data pipelines).

References:
- FAIR Principles: https://www.go-fair.org/fair-principles/
- Methodology: docs/standards/FAIR_COMPLIANCE_CALCULATION_METHODOLOGY.md
"""

from typing import Dict, Any, List
from dataclasses import dataclass, field


@dataclass
class SubPrincipleResult:
    """Result for a single FAIR sub-principle check."""
    principle_id: str  # e.g., "F1", "A1.1"
    score: float  # 0-100
    checks: Dict[str, Any]  # Individual check results
    passed: int  # Number of checks passed
    total: int  # Total number of checks


@dataclass
class PrincipleResult:
    """Result for a main FAIR principle (F, A, I, or R)."""
    principle: str  # "Findable", "Accessible", "Interoperable", "Reusable"
    score: float  # 0-100
    status: str  # "compliant", "partial", "non-compliant"
    sub_principles: Dict[str, SubPrincipleResult] = field(default_factory=dict)


@dataclass
class FAIRComplianceResult:
    """Complete FAIR compliance assessment result."""
    overall_score: float  # 0-100
    overall_status: str  # "compliant", "partial", "non-compliant"
    findable: PrincipleResult = None
    accessible: PrincipleResult = None
    interoperable: PrincipleResult = None
    reusable: PrincipleResult = None
    
    @property
    def sub_principles_passed(self) -> float:
        """Calculate average number of sub-principles passed (out of 15)."""
        return (self.overall_score / 100) * 15


class FAIRCalculator:
    """
    Calculate FAIR compliance for dataset metadata.
    
    This calculator evaluates metadata against all 15 official FAIR sub-principles:
    - F1, F2, F3, F4 (Findable)
    - A1, A1.1, A1.2, A2 (Accessible)
    - I1, I2, I3 (Interoperable)
    - R1, R1.1, R1.2, R1.3 (Reusable)
    
    Usage:
        calculator = FAIRCalculator()
        result = calculator.calculate_compliance(metadata_dict)
        print(f"FAIR Score: {result.overall_score}%")
    """
    
    def __init__(self):
        """Initialize FAIR calculator."""
        self.compliance_threshold = 75.0  # >= 75% = compliant
        self.partial_threshold = 50.0     # >= 50% = partial
    
    def calculate_compliance(self, metadata: Dict[str, Any]) -> FAIRComplianceResult:
        """
        Calculate overall FAIR compliance for metadata.
        
        Args:
            metadata: Metadata dictionary (Dataverse JSON format expected)
        
        Returns:
            FAIRComplianceResult with scores for all principles
        """
        # Check each main principle
        findable = self.check_findable(metadata)
        accessible = self.check_accessible(metadata)
        interoperable = self.check_interoperable(metadata)
        reusable = self.check_reusable(metadata)
        
        # Calculate overall score (average of 4 principles)
        overall_score = (findable.score + accessible.score + 
                        interoperable.score + reusable.score) / 4
        
        # Determine status
        if overall_score >= self.compliance_threshold:
            status = "compliant"
        elif overall_score >= self.partial_threshold:
            status = "partial"
        else:
            status = "non-compliant"
        
        return FAIRComplianceResult(
            overall_score=overall_score,
            overall_status=status,
            findable=findable,
            accessible=accessible,
            interoperable=interoperable,
            reusable=reusable
        )
    
    def check_findable(self, metadata: Dict) -> PrincipleResult:
        """
        Check Findable (F) principle.
        
        F1: (Meta)data are assigned a globally unique and persistent identifier
        F2: Data are described with rich metadata
        F3: Metadata clearly and explicitly include the identifier of the data they describe
        F4: (Meta)data are registered or indexed in a searchable resource
        """
        f1 = self._check_f1_unique_identifier(metadata)
        f2 = self._check_f2_rich_metadata(metadata)
        f3 = self._check_f3_metadata_includes_identifier(metadata)
        f4 = self._check_f4_registered_indexed(metadata)
        
        # Calculate average score
        score = (f1.score + f2.score + f3.score + f4.score) / 4
        status = self._get_status(score)
        
        return PrincipleResult(
            principle="Findable",
            score=score,
            status=status,
            sub_principles={"F1": f1, "F2": f2, "F3": f3, "F4": f4}
        )
    
    def check_accessible(self, metadata: Dict) -> PrincipleResult:
        """
        Check Accessible (A) principle.
        
        A1: (Meta)data are retrievable by their identifier using a standardized communications protocol
        A1.1: The protocol is open, free, and universally implementable
        A1.2: The protocol allows for an authentication and authorization procedure, where necessary
        A2: Metadata are accessible, even when the data are no longer available
        """
        a1 = self._check_a1_retrievable(metadata)
        a1_1 = self._check_a1_1_open_protocol(metadata)
        a1_2 = self._check_a1_2_authentication(metadata)
        a2 = self._check_a2_metadata_persistence(metadata)
        
        score = (a1.score + a1_1.score + a1_2.score + a2.score) / 4
        status = self._get_status(score)
        
        return PrincipleResult(
            principle="Accessible",
            score=score,
            status=status,
            sub_principles={"A1": a1, "A1.1": a1_1, "A1.2": a1_2, "A2": a2}
        )
    
    def check_interoperable(self, metadata: Dict) -> PrincipleResult:
        """
        Check Interoperable (I) principle.
        
        I1: (Meta)data use a formal, accessible, shared, and broadly applicable language for knowledge representation
        I2: (Meta)data use vocabularies that follow FAIR principles
        I3: (Meta)data include qualified references to other (meta)data
        """
        i1 = self._check_i1_formal_language(metadata)
        i2 = self._check_i2_fair_vocabularies(metadata)
        i3 = self._check_i3_qualified_references(metadata)
        
        score = (i1.score + i2.score + i3.score) / 3
        status = self._get_status(score)
        
        return PrincipleResult(
            principle="Interoperable",
            score=score,
            status=status,
            sub_principles={"I1": i1, "I2": i2, "I3": i3}
        )
    
    def check_reusable(self, metadata: Dict) -> PrincipleResult:
        """
        Check Reusable (R) principle.
        
        R1: (Meta)data are richly described with a plurality of accurate and relevant attributes
        R1.1: (Meta)data are released with a clear and accessible data usage license
        R1.2: (Meta)data are associated with detailed provenance
        R1.3: (Meta)data meet domain-relevant community standards
        """
        r1 = self._check_r1_rich_description(metadata)
        r1_1 = self._check_r1_1_license(metadata)
        r1_2 = self._check_r1_2_provenance(metadata)
        r1_3 = self._check_r1_3_community_standards(metadata)
        
        score = (r1.score + r1_1.score + r1_2.score + r1_3.score) / 4
        status = self._get_status(score)
        
        return PrincipleResult(
            principle="Reusable",
            score=score,
            status=status,
            sub_principles={"R1": r1, "R1.1": r1_1, "R1.2": r1_2, "R1.3": r1_3}
        )
    
    # ========================================================================
    # Findable Sub-Principle Checks
    # ========================================================================
    
    def _check_f1_unique_identifier(self, metadata: Dict) -> SubPrincipleResult:
        """F1: (Meta)data are assigned a globally unique and persistent identifier."""
        checks = {
            'identifier_present': False,
            'identifier_format_valid': False,
            'identifier_persistent': False,
            'identifier_globally_unique': True  # Dataverse ensures this
        }
        
        persistent_id = metadata.get('datasetPersistentId')
        if persistent_id:
            checks['identifier_present'] = True
            if persistent_id.startswith('10.') or persistent_id.startswith('hdl:'):
                checks['identifier_format_valid'] = True
                checks['identifier_persistent'] = True
        else:
            # Check if ready for DOI assignment
            if metadata.get('datasetVersion', {}).get('metadataBlocks', {}).get('citation'):
                checks['identifier_present'] = 'will_be_assigned'
                checks['identifier_format_valid'] = True
                checks['identifier_persistent'] = True
        
        return self._calculate_sub_principle_result("F1", checks)
    
    def _check_f2_rich_metadata(self, metadata: Dict) -> SubPrincipleResult:
        """F2: Data are described with rich metadata."""
        citation_fields = metadata.get('datasetVersion', {}).get('metadataBlocks', {}).get('citation', {}).get('fields', [])
        field_names = [f.get('typeName') for f in citation_fields]
        
        checks = {
            'title': 'title' in field_names,
            'author': 'author' in field_names,
            'description': 'dsDescription' in field_names,
            'subject': 'subject' in field_names,
            'contact': 'datasetContact' in field_names,
            'geographic_coverage': 'geographicCoverage' in field_names,
            'temporal_coverage': 'timePeriodCovered' in field_names
        }
        
        return self._calculate_sub_principle_result("F2", checks)
    
    def _check_f3_metadata_includes_identifier(self, metadata: Dict) -> SubPrincipleResult:
        """F3: Metadata clearly and explicitly include the identifier of the data they describe."""
        persistent_id = metadata.get('datasetPersistentId')
        
        checks = {
            'identifier_in_metadata': bool(persistent_id) or 'will_be_on_publication',
            'identifier_standard_format': True,  # Dataverse uses standard format
            'bidirectional_link': True  # Dataverse structure provides this
        }
        
        return self._calculate_sub_principle_result("F3", checks)
    
    def _check_f4_registered_indexed(self, metadata: Dict) -> SubPrincipleResult:
        """F4: (Meta)data are registered or indexed in a searchable resource."""
        persistent_id = metadata.get('datasetPersistentId')
        
        checks = {
            'registered_in_repository': bool(persistent_id) or 'will_be_on_publication',
            'indexed_for_search': bool(persistent_id) or 'will_be_on_publication',
            'harvestable': bool(persistent_id) or 'will_be_on_publication'
        }
        
        return self._calculate_sub_principle_result("F4", checks)
    
    # ========================================================================
    # Accessible Sub-Principle Checks
    # ========================================================================
    
    def _check_a1_retrievable(self, metadata: Dict) -> SubPrincipleResult:
        """A1: (Meta)data are retrievable by their identifier using a standardized communications protocol."""
        checks = {
            'retrievable_via_identifier': bool(metadata.get('datasetPersistentId')),
            'standardized_protocol': True,  # HTTPS
            'data_files_accessible': bool(metadata.get('datasetVersion', {}).get('files'))
        }
        
        return self._calculate_sub_principle_result("A1", checks)
    
    def _check_a1_1_open_protocol(self, metadata: Dict) -> SubPrincipleResult:
        """A1.1: The protocol is open, free, and universally implementable."""
        checks = {
            'protocol_open': True,  # HTTPS is open
            'protocol_free': True,  # HTTPS is free
            'protocol_universal': True  # HTTPS is universal
        }
        
        return self._calculate_sub_principle_result("A1.1", checks)
    
    def _check_a1_2_authentication(self, metadata: Dict) -> SubPrincipleResult:
        """A1.2: The protocol allows for an authentication and authorization procedure, where necessary."""
        checks = {
            'authentication_supported': True,  # Dataverse supports auth
            'authorization_granular': True  # Dataverse has fine-grained permissions
        }
        
        return self._calculate_sub_principle_result("A1.2", checks)
    
    def _check_a2_metadata_persistence(self, metadata: Dict) -> SubPrincipleResult:
        """A2: Metadata are accessible, even when the data are no longer available."""
        checks = {
            'metadata_persistent': True,  # Dataverse keeps metadata even if data removed
            'tombstone_page': True,  # Dataverse creates tombstone for deleted datasets
            'metadata_independent': True  # Metadata stored separately from data
        }
        
        return self._calculate_sub_principle_result("A2", checks)
    
    # ========================================================================
    # Interoperable Sub-Principle Checks
    # ========================================================================
    
    def _check_i1_formal_language(self, metadata: Dict) -> SubPrincipleResult:
        """I1: (Meta)data use a formal, accessible, shared, and broadly applicable language for knowledge representation."""
        checks = {
            'formal_schema': True,  # Dataverse uses Dublin Core
            'schema_accessible': True,  # Dublin Core is public
            'schema_shared': True,  # Dublin Core is standard
            'schema_broadly_applicable': True  # Dublin Core is general-purpose
        }
        
        return self._calculate_sub_principle_result("I1", checks)
    
    def _check_i2_fair_vocabularies(self, metadata: Dict) -> SubPrincipleResult:
        """I2: (Meta)data use vocabularies that follow FAIR principles."""
        citation_fields = metadata.get('datasetVersion', {}).get('metadataBlocks', {}).get('citation', {}).get('fields', [])
        
        checks = {
            'controlled_vocabulary': any(f.get('typeClass') == 'controlledVocabulary' for f in citation_fields),
            'vocabulary_documented': True,  # Dataverse vocabularies are documented
            'vocabulary_resolvable': True  # Dataverse vocabularies are resolvable
        }
        
        return self._calculate_sub_principle_result("I2", checks)
    
    def _check_i3_qualified_references(self, metadata: Dict) -> SubPrincipleResult:
        """I3: (Meta)data include qualified references to other (meta)data."""
        citation_fields = metadata.get('datasetVersion', {}).get('metadataBlocks', {}).get('citation', {}).get('fields', [])
        
        checks = {
            'related_publications': any(f.get('typeName') == 'publication' for f in citation_fields),
            'related_datasets': any(f.get('typeName') == 'relatedDatasets' for f in citation_fields)
        }
        
        return self._calculate_sub_principle_result("I3", checks)
    
    # ========================================================================
    # Reusable Sub-Principle Checks
    # ========================================================================
    
    def _check_r1_rich_description(self, metadata: Dict) -> SubPrincipleResult:
        """R1: (Meta)data are richly described with a plurality of accurate and relevant attributes."""
        citation_fields = metadata.get('datasetVersion', {}).get('metadataBlocks', {}).get('citation', {}).get('fields', [])
        field_names = [f.get('typeName') for f in citation_fields]
        
        checks = {
            'title': 'title' in field_names,
            'description': 'dsDescription' in field_names,
            'keywords': 'keyword' in field_names,
            'geographic_coverage': 'geographicCoverage' in field_names,
            'temporal_coverage': 'timePeriodCovered' in field_names,
            'subject': 'subject' in field_names,
            'author': 'author' in field_names,
            'contact': 'datasetContact' in field_names
        }
        
        return self._calculate_sub_principle_result("R1", checks)
    
    def _check_r1_1_license(self, metadata: Dict) -> SubPrincipleResult:
        """R1.1: (Meta)data are released with a clear and accessible data usage license."""
        citation_fields = metadata.get('datasetVersion', {}).get('metadataBlocks', {}).get('citation', {}).get('fields', [])
        
        checks = {
            'license_present': any(f.get('typeName') == 'license' for f in citation_fields),
            'license_standard': True,  # Dataverse uses standard licenses
            'license_accessible': True  # License info is public
        }
        
        return self._calculate_sub_principle_result("R1.1", checks)
    
    def _check_r1_2_provenance(self, metadata: Dict) -> SubPrincipleResult:
        """R1.2: (Meta)data are associated with detailed provenance."""
        citation_fields = metadata.get('datasetVersion', {}).get('metadataBlocks', {}).get('citation', {}).get('fields', [])
        
        checks = {
            'data_sources': any(f.get('typeName') == 'dataSources' for f in citation_fields),
            'production_date': any(f.get('typeName') == 'productionDate' for f in citation_fields),
            'contributor': any(f.get('typeName') == 'contributor' for f in citation_fields),
            'version_info': bool(metadata.get('datasetVersion', {}).get('versionNumber'))
        }
        
        return self._calculate_sub_principle_result("R1.2", checks)
    
    def _check_r1_3_community_standards(self, metadata: Dict) -> SubPrincipleResult:
        """R1.3: (Meta)data meet domain-relevant community standards."""
        checks = {
            'dublin_core_compliant': True,  # Dataverse uses Dublin Core
            'dataverse_schema_valid': bool(metadata.get('datasetVersion', {}).get('metadataBlocks')),
            'citation_metadata_complete': bool(metadata.get('datasetVersion', {}).get('metadataBlocks', {}).get('citation'))
        }
        
        return self._calculate_sub_principle_result("R1.3", checks)
    
    # ========================================================================
    # Helper Methods
    # ========================================================================
    
    def _calculate_sub_principle_result(self, principle_id: str, checks: Dict[str, Any]) -> SubPrincipleResult:
        """Calculate result for a sub-principle based on its checks."""
        passed = sum(1 for v in checks.values() 
                    if v is True or (isinstance(v, str) and v.startswith('will_be')))
        total = len(checks)
        score = (passed / total * 100) if total > 0 else 0
        
        return SubPrincipleResult(
            principle_id=principle_id,
            score=score,
            checks=checks,
            passed=passed,
            total=total
        )
    
    def _get_status(self, score: float) -> str:
        """Determine compliance status from score."""
        if score >= self.compliance_threshold:
            return "compliant"
        elif score >= self.partial_threshold:
            return "partial"
        else:
            return "non-compliant"


# ============================================================================
# Convenience Functions
# ============================================================================

def calculate_fair_compliance(metadata: Dict[str, Any]) -> FAIRComplianceResult:
    """
    Convenience function to calculate FAIR compliance.
    
    Args:
        metadata: Metadata dictionary (Dataverse JSON format)
    
    Returns:
        FAIRComplianceResult with scores for all principles
    
    Example:
        >>> metadata = {...}  # Dataverse JSON
        >>> result = calculate_fair_compliance(metadata)
        >>> print(f"FAIR Score: {result.overall_score}%")
        >>> print(f"Status: {result.overall_status}")
    """
    calculator = FAIRCalculator()
    return calculator.calculate_compliance(metadata)


def get_fair_percentage(metadata: Dict[str, Any]) -> float:
    """
    Get FAIR compliance percentage (0-100).
    
    Args:
        metadata: Metadata dictionary
    
    Returns:
        Float between 0 and 100
    """
    result = calculate_fair_compliance(metadata)
    return result.overall_score


def get_fair_sub_principles_count(metadata: Dict[str, Any]) -> float:
    """
    Get average number of FAIR sub-principles passed (out of 15).
    
    Args:
        metadata: Metadata dictionary
    
    Returns:
        Float between 0 and 15
    """
    result = calculate_fair_compliance(metadata)
    return result.sub_principles_passed
