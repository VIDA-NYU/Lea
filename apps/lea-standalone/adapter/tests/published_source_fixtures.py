"""Real Stage 6 publication for domain tests; never a browser callback mock."""
from app import db,source_publications


def publish_for(formalization_id,source,*,document='doc',files=None,coverage='partial'):
    with db.write() as conn:
        prior=conn.execute('select generation from source_publishers where document_id=?',(document,)).fetchone()
        authority=source_publications.publisher(source_publications.PublisherRequest(document_id=document,client_id='fixture',expected_generation=prior[0] if prior else 0))
        return source_publications.publish(source_publications.PublicationRequest(document_id=document,client_id='fixture',
            generation=authority['generation'],sequence=authority['sequence']+1,formalization_id=formalization_id,
            source_bundle=source,files=files or {},coverage=coverage))['source_context_ref']
