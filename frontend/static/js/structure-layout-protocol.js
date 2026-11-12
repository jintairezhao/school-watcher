/* Experimental layout protocol. A result supplies geometry, never structural facts. */
import {layoutCompoundStructure} from './structure-layout.js';

export function computeStructureLayout(message) {
    const {generation,structureToken,signature}=message||{};
    const identity={generation,structureToken,signature};
    if(message?.kind!=='layout'||!Number.isSafeInteger(generation)||generation<1
        ||typeof structureToken!=='string'||!structureToken||typeof signature!=='string'||!signature)
        return {...identity,kind:'failed',error:'invalid_layout_request'};
    try {
        const result=layoutCompoundStructure(message.input,message.options||{});
        return {...identity,kind:'completed',result};
    } catch(error) {
        return {...identity,kind:'failed',error:error instanceof Error?error.message:'layout_failed'};
    }
}
