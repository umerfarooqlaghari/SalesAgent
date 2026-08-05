import logging
from typing import Any, Dict, List

from adapter_hub.rag_retrievers.vector_db import vector_db_client

logger = logging.getLogger(__name__)

class RAGRetriever:
    """
    RAG Retriever service. Enforces multi-tenant query boundaries and
    applies a re-ranking model to optimize context retrieval.
    """
    
    def __init__(self, re_rank_boost_factor: float = 0.2):
        self.re_rank_boost_factor = re_rank_boost_factor

    async def retrieve(
        self, 
        tenant_id: str, 
        agent_id: str, 
        query: str, 
        top_k: int = 5,
        min_score: float = 0.05
    ) -> List[Dict[str, Any]]:
        """
        Query vector index and re-rank results based on exact term overlap.
        """
        # 1. Enforce scoping during vector search
        raw_results = await vector_db_client.query_vector_space(tenant_id, agent_id, query, top_k=top_k * 2)
        
        if not raw_results:
            return []
            
        # 2. Re-ranker step: calculate exact keyword matches and boost scores
        re_ranked_results = []
        query_words = set(query.lower().split())
        
        for res in raw_results:
            score = res["score"]
            text_lower = res["text"].lower()
            
            # Count how many query words are in the document text
            matches = sum(1 for word in query_words if word in text_lower)
            
            # Compute boost (if query words match, boost score)
            boost = (matches / len(query_words)) * self.re_rank_boost_factor if query_words else 0.0
            new_score = score + boost
            
            # Filter by score threshold
            if new_score >= min_score:
                res_copy = res.copy()
                res_copy["original_score"] = score
                res_copy["score"] = round(new_score, 4)
                re_ranked_results.append(res_copy)
                
        # 3. Sort again based on boosted scores
        re_ranked_results.sort(key=lambda x: x["score"], reverse=True)
        return re_ranked_results[:top_k]

rag_retriever = RAGRetriever()
